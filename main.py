import asyncio
import logging
import sys
from typing import List

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from sqlalchemy import text

import config
from database.session import engine, AsyncSessionLocal
from middlewares.throttling import ThrottlingMiddleware

# Импорт всех 12 роутеров
from handlers import (
    admin,
    start,
    find,
    emergency,
    menu,
    payments,
    profile,
    review,
    contest,
    notifications,
    inline,
    common,
)

# Импорт всех фоновых служб
from services.data_collector import data_collector_worker
from services.radar import friday_radar_worker
from services.fuelprice_parser import fuel_price_parser_worker
from services.subscription import subscription_expiration_worker
from services.notifications import price_alert_worker
from services.pro_notifications import pro_reminder_worker
from services.address_updater import address_updater_worker

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("BinzoLife")


async def init_database():
    """Гарантирует инициализацию таблиц базы данных при первом запуске."""
    async with AsyncSessionLocal() as db:
        try:
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
                );
                CREATE INDEX IF NOT EXISTS idx_fuel_avail_lookup 
                ON station_current_fuel (station_id, fuel_type, availability);
            """))
            await db.commit()
            logger.info("Таблицы базы данных успешно проверены и готовы к работе.")
        except Exception as e:
            await db.rollback()
            logger.warning(f"Инициализация таблиц БД: {e}")


async def run_supervised(coro_fn, task_name: str):
    """
    Супервизор фоновых задач: изолирует ошибки и перезапускает упавший сервис,
    гарантируя, что бот никогда не прекратит отвечать пользователям в Telegram.
    """
    while True:
        try:
            await coro_fn()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Сбой фонового сервиса [{task_name}]: {e}. Перезапуск через 30 сек.")
            await asyncio.sleep(30)


async def main():
    logger.info("Инициализация и запуск BinzoLife Bot...")
    await init_database()

    # Инициализация бота и диспетчера
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    # Подключение Middlewares защиты от флуда
    dp.message.middleware(ThrottlingMiddleware(limit=0.5))
    dp.callback_query.middleware(ThrottlingMiddleware(limit=0.3))

    # Регистрация всех хендлеров
    dp.include_router(admin.router)
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

    # Регистрация фоновых задач
    tasks: List[asyncio.Task] = [
        asyncio.create_task(run_supervised(data_collector_worker, "DataCollector")),
        asyncio.create_task(run_supervised(lambda: friday_radar_worker(bot), "FridayRadar")),
        asyncio.create_task(run_supervised(fuel_price_parser_worker, "FuelPriceParser")),
        asyncio.create_task(run_supervised(lambda: subscription_expiration_worker(bot), "Subscription")),
        asyncio.create_task(run_supervised(lambda: price_alert_worker(bot), "PriceAlerts")),
        asyncio.create_task(run_supervised(lambda: pro_reminder_worker(bot), "ProReminders")),
        asyncio.create_task(run_supervised(address_updater_worker, "AddressUpdater")),
    ]

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Бот BinzoLife успешно запущен и слушает входящие сообщения Telegram!")
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
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        await engine.dispose()
        logger.info("Бот BinzoLife штатно остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
