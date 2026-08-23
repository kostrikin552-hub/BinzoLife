import sys
import logging
import asyncio
from datetime import datetime

# ---- НАСТРОЙКА ЛОГИРОВАНИЯ (ДО ВСЕГО) ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Диагностический вывод, чтобы убедиться, что код запускается
print("=== STARTING BOT (main.py executed) ===", flush=True)
logger.info("=== MAIN.PY STARTED ===")

from aiogram import Bot, Dispatcher, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web
from sqlalchemy import text, func
from sqlalchemy.exc import ProgrammingError

from config import settings
from database.session import engine, AsyncSessionLocal
from database.models import Base, City, Station, FuelPrice, AvailabilityReport, FuelType, AvailabilityStatus, SourceType
from handlers import start, menu, find, profile, admin, notifications, common, payments, review, emergency
from services.notifications import check_notifications
from services.fuel import refresh_prices
from database.crud import expire_old_prices, expire_old_availability, check_and_award_achievements
from database.session import AsyncSessionLocal

logger.info("Импорты выполнены")

bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
current_bot = None

logger.info("Бот и диспетчер созданы")

# ---- РЕГИСТРАЦИЯ РОУТЕРОВ ----
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

logger.info("Все роутеры зарегистрированы")

# ---- ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ДЛЯ ДИАГНОСТИКИ CALLBACK ----
@dp.callback_query()
async def catch_all_callbacks(callback: types.CallbackQuery):
    logger.info(f"🔔 ГЛОБАЛЬНЫЙ CALLBACK: {callback.data} от {callback.from_user.id}")
    await callback.answer("Диагностика: callback получен, но обработан глобально")

# ---------- HTTP обработчики ----------
async def health_handler(request):
    return web.Response(text='{"status":"ok"}', content_type='application/json')

async def tasks_notifications_handler(request):
    token = request.headers.get("X-Internal-Token")
    logger.info("Получен запрос на /internal/tasks/notifications")
    if token != settings.INTERNAL_TOKEN:
        logger.warning("Неверный токен для уведомлений")
        return web.Response(status=403, text="Forbidden")
    await check_notifications()
    return web.Response(text='{"status":"notifications_checked"}', content_type='application/json')

async def tasks_prices_handler(request):
    token = request.headers.get("X-Internal-Token")
    try:
        await bot.send_message(settings.ADMIN_ID, f"🔔 Получен запрос на /internal/tasks/prices, токен: {token[:6] if token else 'None'}...")
    except Exception as e:
        logger.error(f"Не удалось отправить уведомление: {e}")
    if token != settings.INTERNAL_TOKEN:
        logger.warning("Неверный токен для парсинга")
        try:
            await bot.send_message(settings.ADMIN_ID, "❌ Неверный токен для парсинга")
        except:
            pass
        return web.Response(status=403, text="Forbidden")
    try:
        await bot.send_message(settings.ADMIN_ID, "✅ Токен верный, запускаем парсинг")
    except:
        pass
    logger.info("Токен верный, запускаем refresh_prices")
    try:
        await refresh_prices()
        logger.info("refresh_prices завершена успешно")
        try:
            await bot.send_message(settings.ADMIN_ID, "✅ Парсинг завершён успешно")
        except:
            pass
        return web.Response(text='{"status":"done"}', content_type='application/json')
    except Exception as e:
        error_msg = f"❌ Ошибка парсинга: {e}"
        logger.error(error_msg)
        import traceback
        logger.error(traceback.format_exc())
        try:
            await bot.send_message(settings.ADMIN_ID, error_msg)
        except:
            pass
        return web.Response(text='{"status":"error"}', content_type='application/json')

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
    await add_column_if_not_exists("fuel_prices", "is_fresh", "BOOLEAN", "TRUE")
    await add_column_if_not_exists("availability_reports", "is_fresh", "BOOLEAN", "TRUE")
    logger.info("Обновление схемы БД завершено")

# ---------- Фоновые задачи ----------
async def expire_old_data_periodically():
    while True:
        await asyncio.sleep(1800)
        try:
            async with AsyncSessionLocal() as db:
                await expire_old_prices(db, hours=12)
                await expire_old_availability(db, hours=2)
            logger.info("Устаревшие данные помечены is_fresh=False")
        except Exception as e:
            logger.error(f"Ошибка в expire_old_data_periodically: {e}")

async def check_achievements_periodically():
    while True:
        await asyncio.sleep(3600)
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
                ("Газпромнефть 201", "Газпромнефть", "ул. Мичурина 30Г", 55.99584, 92.97174, 67.59),
                ("Газпромнефть 210", "Газпромнефть", "ул. Гусарова 12Г", 56.02724, 92.76592, 67.59),
                ("Газпромнефть 204", "Газпромнефть", "ул. Авиаторов 2А/1", 56.0459, 92.9235, 67.59),
                ("Газпромнефть 481", "Газпромнефть", "ул. Шахтеров 25А", 56.03542, 92.89036, 87.59),
                ("Лукойл (Кецховели)", "Лукойл", "ул. Ладо Кецховели 45", 56.0142, 92.8133, 84.9),
                ("Лукойл (Волжская)", "Лукойл", "ул. Волжская 63А", 55.9928, 92.9984, 84.9),
                ("Лукойл (2-я Брянская)", "Лукойл", "ул. 2-я Брянская 6Г", 56.0387, 92.8414, 84.9),
                ("Лукойл (Брянская)", "Лукойл", "ул. Брянская 4", 56.0202, 92.8754, 84.9),
                ("Лукойл (Монтажника)", "Лукойл", "ул. Монтажника 24Б", 55.9827, 92.9487, 84.9),
                ("Лукойл (60 лет Октября)", "Лукойл", "ул. 60 лет Октября 161Г", 55.9879, 92.9205, 84.9),
                ("КрасноярскНП (Взлетная)", "КрасноярскНП", "ул. Взлетная 50", 56.0342, 92.8943, 94.0),
                ("КрасноярскНП (Республики)", "КрасноярскНП", "ул. Республики 4", 56.0165, 92.8521, 94.0),
                ("КрасноярскНП (Затонская)", "КрасноярскНП", "ул. Затонская 11д", 55.9948, 92.9261, 94.0),
                ("КрасноярскНП (Тихий)", "КрасноярскНП", "пер. Тихий 1а/1", 56.0127, 92.9510, 94.0),
                ("КрасноярскНП (Маерчака)", "КрасноярскНП", "ул. Маерчака 52а", 56.0184, 92.8672, 94.0),
                ("КрасноярскНП (Грунтовая)", "КрасноярскНП", "ул. Грунтовая 24А", 55.9856, 92.9540, 94.0),
                ("КрасноярскНП (Мичурина)", "КрасноярскНП", "ул. Мичурина 75", 55.9867, 92.9775, 94.0),
                ("КрасноярскНП (Шахтеров)", "КрасноярскНП", "ул. Шахтеров 18", 56.0320, 92.8915, 94.0),
                ("Кит", "Кит", "пер. Телевизорный 4", 56.0247, 92.7879, 71.35),
                ("ОПТИ 2429", "ОПТИ", "пер. Телевизорный 4", 56.0247, 92.7879, 95.9),
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
                    recorded_at=func.now()
                )
                db.add(price_entry)
                availability = AvailabilityReport(
                    station_id=station.id,
                    fuel_type=FuelType.AI_95,
                    status=AvailabilityStatus.GREEN,
                    source=SourceType.ADMIN,
                    confidence=0.9,
                    recorded_at=func.now()
                )
                db.add(availability)
            await db.commit()
            logger.info(f"Загружено {len(stations_data)} станций в Красноярск")
        else:
            logger.info(f"В городе уже есть {count} станций, пропускаем загрузку.")

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
    asyncio.create_task(expire_old_data_periodically())
    asyncio.create_task(check_achievements_periodically())
    logger.info("Бот запущен, фоновые задачи активны")
    try:
        await bot.send_message(settings.ADMIN_ID, "✅ Бот успешно запущен и готов к работе!")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение админу: {e}")

async def on_shutdown():
    global current_bot
    if current_bot:
        await current_bot.session.close()
        logger.info("Сессия бота закрыта")
    await engine.dispose()
    logger.info("Бот остановлен")

# ---------- Запуск с повторными попытками ----------
async def start_bot_with_retry():
    global current_bot
    max_retries = 30
    retry_delay = 3.0
    await asyncio.sleep(10)
    for attempt in range(max_retries):
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            await asyncio.sleep(1)
            try:
                await bot.get_updates(offset=-1, timeout=0)
            except:
                pass
            await asyncio.sleep(1)
            logger.info(f"Запуск polling, попытка {attempt+1}/{max_retries}")
            await dp.start_polling(bot)
            break
        except Exception as e:
            error_str = str(e)
            if "Conflict" in error_str or "terminated by other getUpdates" in error_str:
                logger.warning(f"Конфликт, попытка {attempt+1}/{max_retries}, пауза {retry_delay:.2f} сек")
                await asyncio.sleep(retry_delay)
                retry_delay *= 1.5
                continue
            else:
                logger.error(f"Неизвестная ошибка: {e}")
                raise

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
    try:
        await start_bot_with_retry()
    finally:
        await runner.cleanup()
        await engine.dispose()

if __name__ == "__main__":
    logger.info("=== Блок __main__ выполняется ===")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
