import sys
import logging
import asyncio
import os
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logging.getLogger("aiogram.dispatcher").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

print("=== STARTING BOT (main.py executed) ===", flush=True)
logger.info("=== MAIN.PY STARTED ===")

from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web
from sqlalchemy import text

from config import settings
from database.session import engine, AsyncSessionLocal
from database.models import Base
from handlers import (
    start, menu, find, profile, admin, notifications, common, payments, 
    review, emergency, contest
)
from services.notifications import check_notifications
from services.fuel import refresh_prices
from database.crud import (
    expire_old_prices, expire_old_availability, check_and_award_achievements,
    reset_daily_views
)
from services.address_updater import run_address_updater
from services.pro_notifications import send_pro_expiry_notifications_with_bot
from utils.task_locks import acquire_lock, release_lock, TASK_NAMES

# --- Проверка обязательных переменных ---
if not settings.BOT_TOKEN:
    logger.critical("BOT_TOKEN не задан в переменных окружения!")
    sys.exit(1)
if not settings.ADMIN_ID:
    logger.warning("ADMIN_ID не задан — уведомления админу не будут отправляться")

# --- Создаём бота и диспетчер ---
bot = Bot(
    token=settings.BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
current_bot = None  # будет использоваться в фоновых задачах

# --- Регистрируем роутеры ---
dp.include_router(start.router)
dp.include_router(menu.router)
dp.include_router(find.router)
dp.include_router(profile.router)
dp.include_router(admin.router)
dp.include_router(notifications.router)
dp.include_router(common.router)
dp.include_router(payments.router)
dp.include_router(review.router)
dp.include_router(emergency.router)
dp.include_router(contest.router)

logger.info("Все роутеры зарегистрированы")

# ---------- HTTP-сервер ----------
async def health_handler(request):
    return web.Response(text='{"status":"ok"}', content_type='application/json')

async def webhook_handler(request):
    """Обработчик входящих обновлений от Telegram."""
    try:
        data = await request.json()
        update = types.Update(**data)
        await dp.process_update(update)
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Ошибка в webhook_handler: {e}", exc_info=True)
        return web.Response(status=500, text="Internal Server Error")

async def tasks_notifications_handler(request):
    token = request.headers.get("X-Internal-Token")
    if token != settings.INTERNAL_TOKEN:
        return web.Response(status=403, text="Forbidden")
    lock_acquired = await acquire_lock(TASK_NAMES.NOTIFICATIONS)
    if not lock_acquired:
        return web.Response(text='{"status":"already_running"}', content_type='application/json')
    try:
        await check_notifications()
    finally:
        await release_lock(TASK_NAMES.NOTIFICATIONS)
    return web.Response(text='{"status":"notifications_checked"}', content_type='application/json')

async def tasks_prices_handler(request):
    token = request.headers.get("X-Internal-Token")
    if token != settings.INTERNAL_TOKEN:
        return web.Response(status=403, text="Forbidden")
    lock_acquired = await acquire_lock(TASK_NAMES.PRICES)
    if not lock_acquired:
        return web.Response(text='{"status":"already_running"}', content_type='application/json')
    try:
        await refresh_prices()
    finally:
        await release_lock(TASK_NAMES.PRICES)
    return web.Response(text='{"status":"done"}', content_type='application/json')

def setup_http_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post("/webhook", webhook_handler)
    app.router.add_post("/internal/tasks/notifications", tasks_notifications_handler)
    app.router.add_post("/internal/tasks/prices", tasks_prices_handler)
    return app

# ---------- Функция обновления схемы БД ----------
async def ensure_schema_updates():
    logger.info("Проверка и обновление схемы БД...")
    async def add_column_if_not_exists(table: str, column: str, col_type: str, default: str = ""):
        async with AsyncSessionLocal() as db:
            try:
                await db.execute(text(f"SELECT {column} FROM {table} LIMIT 0"))
                return
            except Exception as e:
                if "does not exist" in str(e) or "UndefinedColumnError" in str(e):
                    await db.rollback()
                    async with AsyncSessionLocal() as db2:
                        try:
                            if default:
                                await db2.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type} DEFAULT {default}"))
                            else:
                                await db2.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
                            await db2.commit()
                            logger.info(f"Добавлена колонка {column} в {table}")
                        except Exception as alter_e:
                            logger.error(f"Не удалось добавить колонку {column} в {table}: {alter_e}")
                            await db2.rollback()
                else:
                    logger.error(f"Ошибка при проверке колонки {column} в {table}: {e}")

    async def create_table_if_not_exists(table_name: str, create_sql: str):
        async with AsyncSessionLocal() as db:
            try:
                await db.execute(text(f"SELECT 1 FROM {table_name} LIMIT 0"))
                return
            except Exception:
                await db.rollback()
                async with AsyncSessionLocal() as db2:
                    try:
                        await db2.execute(text(create_sql))
                        await db2.commit()
                        logger.info(f"Создана таблица {table_name}")
                    except Exception as e:
                        logger.error(f"Не удалось создать таблицу {table_name}: {e}")
                        await db2.rollback()

    # Добавляем колонки (оставляем как есть)
    await add_column_if_not_exists("notifications", "radius_km", "FLOAT")
    await add_column_if_not_exists("users", "total_saved", "FLOAT", "0")
    await add_column_if_not_exists("users", "referral_code", "VARCHAR(20)")
    await add_column_if_not_exists("users", "referred_by", "BIGINT")
    await add_column_if_not_exists("users", "auto_renew", "BOOLEAN", "FALSE")
    await add_column_if_not_exists("users", "first_search_at", "TIMESTAMP WITH TIME ZONE")
    await add_column_if_not_exists("users", "funnel_stage", "INTEGER", "0")
    await add_column_if_not_exists("users", "last_funnel_message_at", "TIMESTAMP WITH TIME ZONE")
    await add_column_if_not_exists("users", "trial_used", "BOOLEAN", "FALSE")
    await add_column_if_not_exists("users", "trial_started", "TIMESTAMP WITH TIME ZONE")
    await add_column_if_not_exists("users", "silent_hours_start", "INTEGER")
    await add_column_if_not_exists("users", "silent_hours_end", "INTEGER")
    await add_column_if_not_exists("stations", "daily_views", "INTEGER", "0")
    await add_column_if_not_exists("stations", "last_view_date", "DATE")

    # Создаём таблицы
    await create_table_if_not_exists("city_slugs", """
        CREATE TABLE city_slugs (
            city_id INTEGER PRIMARY KEY REFERENCES cities(id),
            slug VARCHAR(50) NOT NULL UNIQUE,
            parser_source VARCHAR(50) DEFAULT 'fuelprice',
            is_active BOOLEAN DEFAULT TRUE
        )
    """)
    await create_table_if_not_exists("user_achievements", """
        CREATE TABLE user_achievements (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            achievement_type VARCHAR(50) NOT NULL,
            awarded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            bonus_days_granted INTEGER DEFAULT 0
        )
    """)
    await create_table_if_not_exists("referrals", """
        CREATE TABLE referrals (
            id SERIAL PRIMARY KEY,
            referrer_id INTEGER NOT NULL REFERENCES users(id),
            referred_user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            is_rewarded BOOLEAN DEFAULT FALSE
        )
    """)
    await create_table_if_not_exists("user_economies", """
        CREATE TABLE user_economies (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            station_id INTEGER REFERENCES stations(id),
            price_paid FLOAT NOT NULL,
            city_avg_price FLOAT NOT NULL,
            saved FLOAT NOT NULL,
            recorded_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    await create_table_if_not_exists("geocode_cache", """
        CREATE TABLE geocode_cache (
            id SERIAL PRIMARY KEY,
            lat FLOAT NOT NULL,
            lng FLOAT NOT NULL,
            address VARCHAR(500) NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (lat, lng)
        )
    """)
    await create_table_if_not_exists("pro_notifications_sent", """
        CREATE TABLE pro_notifications_sent (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            notification_type VARCHAR(20) NOT NULL,
            sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (user_id, notification_type)
        )
    """)
    await add_column_if_not_exists("fuel_prices", "is_fresh", "BOOLEAN", "TRUE")
    await add_column_if_not_exists("availability_reports", "is_fresh", "BOOLEAN", "TRUE")

    # Создаём таблицу блокировок (НОВАЯ)
    async with AsyncSessionLocal() as db:
        await db.execute(text("""
            CREATE TABLE IF NOT EXISTS task_locks (
                task_name VARCHAR(50) PRIMARY KEY,
                locked_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                locked_by VARCHAR(100)
            )
        """))
        await db.commit()
    logger.info("Обновление схемы БД завершено")

# ---------- Фоновые задачи (с блокировками) ----------
async def expire_old_data_periodically():
    while True:
        await asyncio.sleep(1800)
        lock_acquired = await acquire_lock(TASK_NAMES.EXPIRE_DATA)
        if not lock_acquired:
            continue
        try:
            async with AsyncSessionLocal() as db:
                await expire_old_prices(db, hours=12)
                await expire_old_availability(db, hours=2)
            logger.info("Устаревшие данные помечены is_fresh=False")
        except Exception as e:
            logger.error(f"Ошибка в expire_old_data_periodically: {e}")
        finally:
            await release_lock(TASK_NAMES.EXPIRE_DATA)

async def check_achievements_periodically():
    while True:
        await asyncio.sleep(3600)
        lock_acquired = await acquire_lock(TASK_NAMES.ACHIEVEMENTS)
        if not lock_acquired:
            continue
        try:
            async with AsyncSessionLocal() as db:
                users_with_reports = await db.execute(
                    text("SELECT DISTINCT user_id FROM availability_reports WHERE user_id IS NOT NULL")
                )
                for (user_id,) in users_with_reports:
                    await check_and_award_achievements(db, user_id)
            logger.info("Достижения проверены")
        except Exception as e:
            logger.error(f"Ошибка в check_achievements_periodically: {e}")
        finally:
            await release_lock(TASK_NAMES.ACHIEVEMENTS)

async def funnel_worker():
    from services.funnel import process_funnel
    while True:
        lock_acquired = await acquire_lock(TASK_NAMES.FUNNEL)
        if not lock_acquired:
            await asyncio.sleep(600)
            continue
        try:
            await process_funnel()
        except Exception as e:
            logger.error(f"Ошибка в funnel_worker: {e}")
        finally:
            await release_lock(TASK_NAMES.FUNNEL)
        await asyncio.sleep(600)

async def reset_views_periodically():
    while True:
        await asyncio.sleep(600)
        lock_acquired = await acquire_lock(TASK_NAMES.RESET_VIEWS)
        if not lock_acquired:
            continue
        try:
            async with AsyncSessionLocal() as db:
                await reset_daily_views(db)
            logger.info("Сброс daily_views выполнен")
        except Exception as e:
            logger.error(f"Ошибка сброса daily_views: {e}")
        finally:
            await release_lock(TASK_NAMES.RESET_VIEWS)

async def address_updater_worker():
    from services.address_updater import update_station_addresses
    while True:
        lock_acquired = await acquire_lock(TASK_NAMES.ADDRESS_UPDATER)
        if not lock_acquired:
            await asyncio.sleep(86400)
            continue
        try:
            await update_station_addresses()
        except Exception as e:
            logger.error(f"Ошибка в address_updater: {e}")
        finally:
            await release_lock(TASK_NAMES.ADDRESS_UPDATER)
        await asyncio.sleep(86400)  # 24 часа

async def pro_expiry_notifier():
    while True:
        lock_acquired = await acquire_lock(TASK_NAMES.PRO_NOTIFY)
        if not lock_acquired:
            await asyncio.sleep(3600)
            continue
        try:
            await send_pro_expiry_notifications_with_bot(bot)
        except Exception as e:
            logger.error(f"Ошибка в pro_expiry_notifier: {e}")
        finally:
            await release_lock(TASK_NAMES.PRO_NOTIFY)
        await asyncio.sleep(3600)

# ---------- Загрузка начальных данных ----------
async def seed_initial_data():
    # ... (оставьте как есть, без изменений)
    pass

# ---------- Настройка вебхука ----------
async def set_webhook():
    webhook_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not webhook_url:
        logger.warning("RENDER_EXTERNAL_URL не задан, не могу установить вебхук")
        return False
    webhook_url = webhook_url.rstrip("/") + "/webhook"
    try:
        await bot.delete_webhook()
        await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
        logger.info(f"Вебхук установлен: {webhook_url}")
        return True
    except Exception as e:
        logger.error(f"Ошибка установки вебхука: {e}")
        return False

# ---------- Startup / Shutdown ----------
async def on_startup():
    global current_bot
    current_bot = bot
    logger.info("=== ON_STARTUP CALLED ===")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        logger.info("Таблицы созданы (если не существовали)")
    await ensure_schema_updates()
    await seed_initial_data()
    
    webhook_ok = await set_webhook()
    if not webhook_ok:
        logger.warning("Не удалось установить вебхук, возможно, работаем без вебхуков")
    
    asyncio.create_task(expire_old_data_periodically())
    asyncio.create_task(check_achievements_periodically())
    asyncio.create_task(funnel_worker())
    asyncio.create_task(reset_views_periodically())
    asyncio.create_task(address_updater_worker())
    asyncio.create_task(pro_expiry_notifier())
    
    logger.info("Бот запущен, фоновые задачи активны")
    try:
        await bot.send_message(settings.ADMIN_ID, "✅ Бот успешно запущен и готов к работе!")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение админу: {e}")

async def on_shutdown():
    global current_bot
    if current_bot:
        try:
            await current_bot.session.close()
        except:
            pass
        logger.info("Сессия бота закрыта")
    await engine.dispose()
    logger.info("Бот остановлен")

# ---------- Основная функция ----------
async def main():
    logger.info("=== MAIN() CALLED ===")
    app = setup_http_server()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', settings.PORT)
    await site.start()
    logger.info(f"HTTP сервер запущен на порту {settings.PORT}")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    # В режиме вебхуков мы НЕ используем polling. Просто держим сервер.
    try:
        await asyncio.Event().wait()  # Бесконечное ожидание
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    finally:
        await runner.cleanup()
        await engine.dispose()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
