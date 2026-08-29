import sys
import logging
import asyncio
import re
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logging.getLogger("aiogram.dispatcher").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

print("=== STARTING BOT (main.py executed) ===", flush=True)
logger.info("=== MAIN.PY STARTED ===")

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web
from sqlalchemy import text

from config import settings
from database.session import engine, AsyncSessionLocal
from database.models import Base, City, Station, FuelPrice, AvailabilityReport, FuelType, SourceType, AvailabilityStatus
from handlers import (
    start, menu, find, profile, admin, notifications, common, payments,
    review, emergency, contest
)
from services.notifications import check_notifications
from services.fuel import refresh_prices
from database.crud import (
    expire_old_prices, expire_old_availability, check_and_award_achievements,
    reset_daily_views, aggregate_old_prices
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
current_bot = None

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
    await add_column_if_not_exists("users", "free_searches_today", "INTEGER", "1")
    await add_column_if_not_exists("users", "last_free_search_date", "DATE")

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
        await asyncio.sleep(86400)

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

# ---------- НОВАЯ ЗАДАЧА: агрегация старых данных (раз в неделю) ----------
async def aggregate_old_data_periodically():
    while True:
        await asyncio.sleep(86400 * 7)  # раз в неделю
        lock_acquired = await acquire_lock("aggregate_old_data")
        if not lock_acquired:
            continue
        try:
            async with AsyncSessionLocal() as db:
                await aggregate_old_prices(db, days_threshold=60)
            logger.info("Агрегация старых данных выполнена")
        except Exception as e:
            logger.error(f"Ошибка агрегации старых данных: {e}")
        finally:
            await release_lock("aggregate_old_data")

# ---------- СУПЕРВИЗОР ДЛЯ ФОНОВЫХ ЗАДАЧ ----------
async def supervised_task(coro_func, name: str, restart_delay: int = 30):
    """Обёртка для фоновых задач с автоматическим перезапуском при падении."""
    while True:
        try:
            await coro_func()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.critical(
                f"❌ Фоновый процесс '{name}' упал с ошибкой: {e}. "
                f"Перезапуск через {restart_delay} сек...",
                exc_info=True
            )
            await asyncio.sleep(restart_delay)

# ---------- Загрузка начальных данных ----------
async def seed_initial_data():
    async with AsyncSessionLocal() as db:
        city_updated = False
        city = await db.execute(text("SELECT id, latitude, longitude FROM cities WHERE name = 'Красноярск'"))
        row = city.fetchone()
        if not row:
            new_city = City(
                name="Красноярск",
                region="Красноярский край",
                latitude=56.0109,
                longitude=92.8525,
                is_active=True
            )
            db.add(new_city)
            await db.flush()
            city_id = new_city.id
            city_updated = True
            logger.info("Город Красноярск создан с координатами")
        else:
            city_id = row[0]
            if row[1] is None or row[2] is None:
                await db.execute(
                    text("UPDATE cities SET latitude = :lat, longitude = :lon WHERE id = :id"),
                    {"lat": 56.0109, "lon": 92.8525, "id": city_id}
                )
                city_updated = True
                logger.info("Обновлены координаты для города Красноярск")
        if city_updated:
            await db.commit()

        stations_count = await db.execute(text("SELECT COUNT(*) FROM stations WHERE city_id = :city_id"), {"city_id": city_id})
        count = stations_count.scalar()
        if count == 0:
            stations_data = [
                ("Газпромнефть 349", "Газпромнефть", "ул. 60 лет Октября 105А", 55.9829, 92.8969, 67.59),
            ]
            for name, brand, address, lat, lon, price in stations_data:
                station = Station(
                    city_id=city_id,
                    name=name,
                    brand=brand,
                    address=address,
                    latitude=lat,
                    longitude=lon,
                    is_active=True
                )
                db.add(station)
                await db.flush()
                price_entry = FuelPrice(
                    station_id=station.id,
                    fuel_type=FuelType.AI_95,
                    price=price,
                    source=SourceType.ADMIN,
                    confidence=0.9,
                    recorded_at=datetime.now(timezone.utc)
                )
                db.add(price_entry)
                availability = AvailabilityReport(
                    station_id=station.id,
                    fuel_type=FuelType.AI_95,
                    status=AvailabilityStatus.GREEN,
                    source=SourceType.ADMIN,
                    confidence=0.9,
                    recorded_at=datetime.now(timezone.utc)
                )
                db.add(availability)
            await db.commit()
            logger.info(f"Загружено {len(stations_data)} станций в Красноярск")
        else:
            logger.info(f"В городе уже есть {count} станций, пропускаем загрузку.")

# ---------- Безопасная отправка сообщений ----------
async def send_message_safe(bot: Bot, chat_id: int, text: str, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            await bot.send_message(chat_id, text)
            return
        except Exception as e:
            if "RetryAfter" in str(e) or "flood" in str(e).lower():
                match = re.search(r'retry after (\d+)', str(e), re.IGNORECASE)
                if match:
                    wait = int(match.group(1)) + 1
                else:
                    wait = 5 * (attempt + 1)
                logger.warning(f"Flood control, ждём {wait} сек...")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Ошибка отправки: {e}")
                return
    logger.error(f"Не удалось отправить сообщение после {max_retries} попыток")

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

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Вебхук удалён (если был)")
        await asyncio.sleep(2)
    except Exception as e:
        logger.warning(f"Не удалось удалить вебхук: {e}")

    # Запускаем фоновые задачи через супервизор
    asyncio.create_task(supervised_task(expire_old_data_periodically, "expire_old_data"))
    asyncio.create_task(supervised_task(check_achievements_periodically, "check_achievements"))
    asyncio.create_task(supervised_task(funnel_worker, "funnel_worker"))
    asyncio.create_task(supervised_task(reset_views_periodically, "reset_views"))
    asyncio.create_task(supervised_task(address_updater_worker, "address_updater"))
    asyncio.create_task(supervised_task(pro_expiry_notifier, "pro_expiry_notifier"))
    asyncio.create_task(supervised_task(aggregate_old_data_periodically, "aggregate_old_data"))

    logger.info("Бот запущен, фоновые задачи активны")
    await send_message_safe(bot, settings.ADMIN_ID, "✅ Бот успешно запущен и готов к работе (polling)!")

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

    max_retries = 10
    retry_delay = 5
    for attempt in range(max_retries):
        try:
            logger.info(f"Запуск polling, попытка {attempt+1}/{max_retries}")
            await dp.start_polling(bot)
            break
        except Exception as e:
            if "Conflict" in str(e) or "terminated by other getUpdates" in str(e):
                logger.warning(f"Конфликт polling, попытка {attempt+1}/{max_retries}, ждём {retry_delay} сек...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 30)
            else:
                logger.error(f"Неизвестная ошибка polling: {e}")
                raise
    else:
        logger.critical("Не удалось запустить polling после всех попыток")

    await runner.cleanup()
    await engine.dispose()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
