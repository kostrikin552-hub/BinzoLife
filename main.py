# main.py — ПОЛНАЯ РАБОЧАЯ ВЕРСИЯ ДЛЯ BINZOLIFE
import asyncio
import logging
import sys
from datetime import datetime
from typing import List

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from sqlalchemy import text

# Конфигурация и БД
import config
from database.session import engine, AsyncSessionLocal

# Middlewares
from middlewares.throttling import ThrottlingMiddleware

# Хендлеры (все роутеры бота)
from handlers import (
    start,
    find,
    emergency,
    menu,
    payments,
    profile,
    review,
    contest,
    admin,
    notifications,
    common,
    inline,
)

# Фоновые сервисы
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
logger = logging.getLogger("BinzoLifeBot")


async def init_db():
    """Проверка и автоматическое создание необходимых таблиц/колонок при старте."""
    logger.info("Проверка структуры базы данных PostgreSQL...")
    async with AsyncSessionLocal() as db:
        try:
            # Создание таблицы station_current_fuel для сборщика gdebenz/цен если её нет
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
            """))
            await db.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_st_curr_fuel_lookup 
                ON station_current_fuel (station_id, fuel_type, availability);
            """))
            await db.commit()
            logger.info("Структура базы данных проверена и готова к работе.")
        except Exception as e:
            await db.rollback()
            logger.error(f"Предупреждение при проверке схемы БД: {e}")


async def supervised_task(coro, name: str):
    """
    Супервизор фоновых задач: если фоновый сервис (парсер или радар) 
    ловит неожиданную ошибку сети, он перезапускается через 15 секунд, 
    не роняя и не блокируя самого бота.
    """
    logger.info(f"Запущен фоновый сервис: [{name}]")
    while True:
        try:
            await coro()
        except asyncio.CancelledError:
            logger.info(f"Фоновый сервис [{name}] корректно остановлен.")
            break
        except Exception as e:
            logger.error(f"Ошибка в фоновом сервисе [{name}]: {e}. Перезапуск через 15 сек...")
            await asyncio.sleep(15)


async def main():
    logger.info("=== Запуск BinzoLife Telegram Bot ===")
    
    # 1. Инициализация БД
    await init_db()

    # 2. Инициализация Bot и Dispatcher
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    dp = Dispatcher()

    # 3. Регистрация Middlewares (защита от флуда и троттлинг)
    dp.message.middleware(ThrottlingMiddleware(limit=0.5))
    dp.callback_query.middleware(ThrottlingMiddleware(limit=0.3))

    # 4. Регистрация всех хендлеров
    # ВАЖНО: порядок регистрации важен для корректного перехвата команд
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
    dp.include_router(inline.router)     # Инлайн-поиск в чатах
    dp.include_router(common.router)     # Обработка остальных текстовых кнопок

    # 5. Запуск фоновых воркеров через независимые Tasks (БЕЗ блокировки event loop!)
    background_tasks: List[asyncio.Task] = [
        # Сборщик данных наличия/очередей (gdebenz)
        asyncio.create_task(
            supervised_task(data_collector_worker, "DataCollector"), 
            name="task_data_collector"
        ),
        # Пятничный радар цен
        asyncio.create_task(
            supervised_task(lambda: friday_radar_worker(bot), "FridayRadar"), 
            name="task_friday_radar"
        ),
        # Парсер стел и цен
        asyncio.create_task(
            supervised_task(fuel_price_parser_worker, "FuelPriceParser"), 
            name="task_fuel_parser"
        ),
        # Контроль окончания подписок и триалов PRO
        asyncio.create_task(
            supervised_task(lambda: subscription_expiration_worker(bot), "SubscriptionWorker"), 
            name="task_subscription"
        ),
        # Персональные алерты о снижении цен
        asyncio.create_task(
            supervised_task(lambda: price_alert_worker(bot), "PriceAlerts"), 
            name="task_price_alerts"
        ),
        # PRO-напоминания и онбординг
        asyncio.create_task(
            supervised_task(lambda: pro_reminder_worker(bot), "ProReminders"), 
            name="task_pro_reminders"
        ),
        # Геокодер и обновление адресов АЗС
        asyncio.create_task(
            supervised_task(address_updater_worker, "AddressUpdater"), 
            name="task_address_updater"
        ),
    ]

    logger.info("✅ Все 7 фоновых сервисов и 12 роутеров успешно запущены!")
    logger.info("Бот готов к приёму сообщений пользователей...")

    try:
        # 6. Запуск Long Polling
        # Очищаем очередь старых накопившихся сообщений за время простоя
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "callback_query",
                "inline_query",
                "chosen_inline_result",
                "pre_checkout_query",
                "shipping_query"
            ]
        )
    finally:
        logger.info("Остановка бота: завершение фоновых процессов...")
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)
        await bot.session.close()
        await engine.dispose()
        logger.info("Бот и соединения с БД полностью остановлены.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен вручную.")
