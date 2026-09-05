# main.py — ПОЛНАЯ ФИНАЛЬНАЯ ВЕРСИЯ
import os
import asyncio
import logging
import sys
from typing import List

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiohttp import web
from sqlalchemy import text

import config
from database.session import engine, AsyncSessionLocal
from middlewares.throttling import ThrottlingMiddleware
from middlewares.clear_state import ClearStateOnMenuMiddleware

# Хендлеры (кроме admin)
from handlers import (
    start, find, emergency, menu, payments, profile, review,
    contest, notifications, common, inline,
)
from handlers.admin import router as admin_router

# Фоновые сервисы
from services.data_collector import data_collector_worker
from services.radar import friday_radar_worker
from services.fuelprice_parser import fuel_price_parser_worker
from services.subscription import subscription_expiration_worker
from services.address_updater import address_updater_worker
from database.crud import seed_all_russian_cities, update_city_slugs_from_seed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("BinzoLifeBot")

CRON_SECRET = os.getenv("CRON_SECRET", "7RV4gLekEl0rFhfm2WtyaX58zQpS19")


# ======================== HTTP-СЕРВЕР ========================
async def health_check_handler(request):
    return web.Response(text="OK", status=200)

def check_cron_auth(request) -> bool:
    token = request.query.get("token")
    return token == CRON_SECRET

async def cron_alerts_handler(request):
    if not check_cron_auth(request):
        return web.Response(text="Forbidden", status=403)
    return web.Response(text="Alerts disabled", status=200)

async def cron_subscriptions_handler(request):
    if not check_cron_auth(request):
        return web.Response(text="Forbidden", status=403)
    return web.Response(text="Subscriptions disabled", status=200)

async def start_http_server():
    port = int(os.environ.get("PORT", "10000"))
    host = "0.0.0.0"
    logger.info(f"🚀 Запуск HTTP-сервера на {host}:{port}")
    app = web.Application()
    app.router.add_get("/", health_check_handler)
    app.router.add_get("/health", health_check_handler)
    app.router.add_get("/healthz", health_check_handler)
    app.router.add_get("/cron/alerts", cron_alerts_handler)
    app.router.add_get("/cron/subscriptions", cron_subscriptions_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"✅ HTTP-сервер успешно запущен на {host}:{port}")
    return runner


# ======================== ИНИЦИАЛИЗАЦИЯ БД ========================
async def init_database():
    logger.info("Проверка структуры БД...")
    async with AsyncSessionLocal() as db:
        try:
            # Создание таблицы station_current_fuel
            await db.execute(text("""
                CREATE TABLE IF NOT EXISTS station_current_fuel (
                    station_id BIGINT NOT NULL,
                    fuel_type VARCHAR(16) NOT NULL,
                    price NUMERIC(10, 2),
                    availability VARCHAR(16) DEFAULT 'available',
                    queue_level VARCHAR(16) DEFAULT 'unknown',
                    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    source VARCHAR(32) NOT NULL DEFAULT 'manual',
                    confidence NUMERIC(5, 2) NOT NULL DEFAULT 0.8,
                    PRIMARY KEY (station_id, fuel_type)
                )
            """))
            await db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_fuel_avail_lookup 
                ON station_current_fuel (station_id, fuel_type, availability)
            """))

            # Добавление колонок в users
            await db.execute(text("""
                DO $$ 
                BEGIN 
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='users' AND column_name='timezone_offset') THEN
                        ALTER TABLE users ADD COLUMN timezone_offset INTEGER;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='users' AND column_name='last_lat') THEN
                        ALTER TABLE users ADD COLUMN last_lat FLOAT;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='users' AND column_name='last_lon') THEN
                        ALTER TABLE users ADD COLUMN last_lon FLOAT;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='users' AND column_name='is_active') THEN
                        ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT true;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='users' AND column_name='has_made_first_search') THEN
                        ALTER TABLE users ADD COLUMN has_made_first_search BOOLEAN DEFAULT false;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='users' AND column_name='trial_alert_sent') THEN
                        ALTER TABLE users ADD COLUMN trial_alert_sent BOOLEAN DEFAULT false;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='users' AND column_name='invited_count') THEN
                        ALTER TABLE users ADD COLUMN invited_count INTEGER DEFAULT 0;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='users' AND column_name='emergency_access_until') THEN
                        ALTER TABLE users ADD COLUMN emergency_access_until TIMESTAMPTZ;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='users' AND column_name='first_name') THEN
                        ALTER TABLE users ADD COLUMN first_name VARCHAR(100);
                    END IF;
                END $$;
            """))

            # Добавление колонок в fuel_prices
            await db.execute(text("""
                DO $$ 
                BEGIN 
                    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'fuel_prices') THEN
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                       WHERE table_name='fuel_prices' AND column_name='previous_price') THEN
                            ALTER TABLE fuel_prices ADD COLUMN previous_price NUMERIC(10, 2);
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                       WHERE table_name='fuel_prices' AND column_name='is_fresh') THEN
                            ALTER TABLE fuel_prices ADD COLUMN is_fresh BOOLEAN DEFAULT true;
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                       WHERE table_name='fuel_prices' AND column_name='updated_at') THEN
                            ALTER TABLE fuel_prices ADD COLUMN updated_at TIMESTAMPTZ DEFAULT NOW();
                        END IF;
                        IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                       WHERE table_name='fuel_prices' AND column_name='updated_by_user_id') THEN
                            ALTER TABLE fuel_prices ADD COLUMN updated_by_user_id BIGINT;
                        END IF;
                    END IF;
                END $$;
            """))

            # Добавление колонки slug в cities (если её нет)
            await db.execute(text("""
                DO $$ 
                BEGIN 
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                                   WHERE table_name='cities' AND column_name='slug') THEN
                        ALTER TABLE cities ADD COLUMN slug VARCHAR(50) UNIQUE;
                    END IF;
                END $$;
            """))

            await db.commit()
            logger.info("Структура БД готова.")
        except Exception as e:
            await db.rollback()
            logger.warning(f"Ошибка инициализации БД: {e}")


# ======================== ФОНОВЫЙ ПЛАНИРОВЩИК ПАРСЕРА ========================
async def scheduled_fuel_parser_worker(session_factory):
    await asyncio.sleep(45)
    while True:
        try:
            await fuel_parser.run_daily_parse_all_cities(session_factory)
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as err:
            logger.error(f"Сбой в воркере парсера: {err}")
            await asyncio.sleep(300)


# ======================== СУПЕРВИЗОР ========================
async def run_supervised(coro, task_name: str):
    while True:
        try:
            await coro()
        except asyncio.CancelledError:
            logger.info(f"Фоновый сервис [{task_name}] остановлен.")
            break
        except Exception as e:
            logger.error(f"Сбой [{task_name}]: {e}. Перезапуск через 20 сек.")
            await asyncio.sleep(20)


# ======================== ОСНОВНАЯ ФУНКЦИЯ ========================
async def main():
    logger.info("=== Запуск BinzoLife Bot ===")
    await init_database()

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    # 1. HTTP-сервер
    http_runner = await start_http_server()

    # 2. Middlewares
    dp.message.middleware(ThrottlingMiddleware(limit=0.5))
    dp.callback_query.middleware(ThrottlingMiddleware(limit=0.3))
    dp.message.middleware(ClearStateOnMenuMiddleware())

    # 3. Роутеры
    dp.include_router(start.router)
    dp.include_router(find.router)
    dp.include_router(emergency.router)
    dp.include_router(menu.router)
    dp.include_router(payments.router)
    dp.include_router(profile.router)
    dp.include_router(review.router)
    dp.include_router(contest.router)
    dp.include_router(notifications.router)
    dp.include_router(inline.router)
    dp.include_router(common.router)
    dp.include_router(admin_router)

    # 4. Актуализация базы городов и слагов (ВЫПОЛНЯЕТСЯ ВСЕГДА ПРИ СТАРТЕ)
    async with AsyncSessionLocal() as session:
        try:
            # Шаг А: Проставляем слаги всем уже существующим городам
            updated_slugs = await update_city_slugs_from_seed(session)
            if updated_slugs > 0:
                logger.info(f"✅ Обновлено слагов у существующих городов: {updated_slugs}")

            # Шаг Б: Добавляем все недостающие города из списка 65+ (без дублей)
            added_cities = await seed_all_russian_cities(session)
            if added_cities > 0:
                logger.info(f"✅ Добавлено новых городов России: {added_cities}")

            # Проверяем итоговый статус
            total_cities = await session.execute(text("SELECT COUNT(*) FROM cities WHERE is_active = true"))
            with_slugs = await session.execute(text("SELECT COUNT(*) FROM cities WHERE slug IS NOT NULL AND slug != ''"))
            logger.info(f"📊 Статус городов в БД: Всего активных = {total_cities.scalar()}, Со слагами = {with_slugs.scalar()}")
        except Exception as e:
            logger.error(f"Ошибка при обновлении списка городов: {e}", exc_info=True)

    # 5. Фоновые задачи
    background_tasks: List[asyncio.Task] = [
        asyncio.create_task(run_supervised(data_collector_worker, "DataCollector")),
        asyncio.create_task(run_supervised(lambda: friday_radar_worker(bot), "FridayRadar")),
        asyncio.create_task(run_supervised(fuel_price_parser_worker, "FuelPriceParser")),
        asyncio.create_task(run_supervised(lambda: subscription_expiration_worker(bot), "Subscription")),
        asyncio.create_task(run_supervised(address_updater_worker, "AddressUpdater")),
    ]

    # 6. Polling с ретраями
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Бот готов к запуску polling...")

    max_retries = 10
    retry_delay = 5
    for attempt in range(max_retries):
        try:
            await dp.start_polling(
                bot,
                allowed_updates=[
                    "message",
                    "callback_query",
                    "inline_query",
                    "chosen_inline_result",
                    "pre_checkout_query",
                ]
            )
            break
        except Exception as e:
            if "Conflict" in str(e) or "terminated by other getUpdates" in str(e):
                logger.warning(f"Конфликт polling, попытка {attempt+1}/{max_retries}, ждём {retry_delay} сек...")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 1.5, 30)
            else:
                logger.error(f"Неизвестная ошибка polling: {e}")
                raise

    # 7. Завершение
    for t in background_tasks:
        t.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)
    await http_runner.cleanup()
    await bot.session.close()
    await engine.dispose()
    logger.info("Бот остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")
