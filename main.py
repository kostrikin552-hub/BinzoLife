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

# Импорт фоновых служб и функций задач
from services.data_collector import data_collector_worker
from services.radar import friday_radar_worker
from services.fuelprice_parser import fuel_price_parser_worker
from services.subscription import subscription_expiration_worker, check_expiring_subscriptions
from services.notifications import price_alert_worker, process_price_drop_alerts
from services.pro_notifications import pro_reminder_worker, send_pro_onboarding_reminders
from services.address_updater import address_updater_worker

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - [%(levelname)s] - %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("BinzoLife")

# Секретный токен для защиты вызовов cron (можно переопределить в .env CRON_SECRET)
CRON_SECRET = os.getenv("CRON_SECRET", "7RV4gLekEl0rFhfm2WtyaX58zQpS19")


async def init_database():
    """Гарантирует инициализацию и совместимость колонок базы данных."""
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

            await db.execute(text("""
                DO $$ 
                BEGIN 
                    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'fuel_prices') THEN
                        ALTER TABLE fuel_prices ADD COLUMN IF NOT EXISTS previous_price NUMERIC(10, 2);
                        ALTER TABLE fuel_prices ADD COLUMN IF NOT EXISTS is_fresh BOOLEAN DEFAULT true;
                        ALTER TABLE fuel_prices ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
                    END IF;

                    IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'users') THEN
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT true;
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS is_pro BOOLEAN DEFAULT false;
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_until TIMESTAMPTZ;
                        ALTER TABLE users ADD COLUMN IF NOT EXISTS city_id INTEGER;
                    END IF;
                END $$;
            """))
            await db.commit()
            logger.info("Таблицы базы данных успешно синхронизированы и готовы к работе.")
        except Exception as e:
            await db.rollback()
            logger.warning(f"Инициализация таблиц БД: {e}")


# =====================================================================
# HTTP ОБРАБОТЧИКИ ДЛЯ PING И CRON-JOB
# =====================================================================

async def health_check_handler(request):
    """Обычный пинг для Uptime / Render / cron-job.org (держит бота бодрым)."""
    return web.Response(text="OK: BinzoLife Bot is active", status=200)


def check_cron_auth(request) -> bool:
    """Проверяет секретный токен для защиты cron-заданий."""
    token = request.query.get("token") or request.headers.get("X-Cron-Token")
    return token == CRON_SECRET


async def cron_alerts_handler(request):
    """Эндпоинт для cron-job: запуск проверки цен и рассылки алертов."""
    if not check_cron_auth(request):
        return web.Response(text="Forbidden: Invalid token", status=403)

    bot: Bot = request.app["bot"]
    asyncio.create_task(process_price_drop_alerts(bot))
    return web.Response(text="Success: Price alerts task triggered", status=200)


async def cron_subscription_handler(request):
    """Эндпоинт для cron-job: проверка истекающих подписок и онбординга."""
    if not check_cron_auth(request):
        return web.Response(text="Forbidden: Invalid token", status=403)

    bot: Bot = request.app["bot"]
    asyncio.create_task(check_expiring_subscriptions(bot))
    asyncio.create_task(send_pro_onboarding_reminders(bot))
    return web.Response(text="Success: Subscription check triggered", status=200)


async def start_web_server(bot: Bot):
    """Запускает HTTP сервер с поддержкой Ping и Cron."""
    port_env = os.getenv("PORT") or os.getenv("RENDER_PORT") or "10000"
    port = int(port_env)
    host = "0.0.0.0"

    app = web.Application()
    app["bot"] = bot

    # Маршруты пинга
    app.router.add_get("/", health_check_handler)
    app.router.add_get("/health", health_check_handler)
    app.router.add_get("/healthz", health_check_handler)

    # Маршруты для Cron-job
    app.router.add_get("/cron/alerts", cron_alerts_handler)
    app.router.add_get("/cron/subscriptions", cron_subscription_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"🌐 HTTP сервер запущен на http://{host}:{port} (готов к Ping и Cron).")
    return runner


async def run_supervised(coro_fn, task_name: str):
    """Супервизор фоновых задач."""
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

    # Запуск HTTP сервера для Render и внешнего Cron
    http_runner = None
    try:
        http_runner = await start_web_server(bot)
    except Exception as e:
        logger.error(f"Ошибка запуска HTTP-сервера: {e}")

    # Подключение Middlewares
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

    # Фоновые службы (работают параллельно внутри бота)
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
        if http_runner:
            await http_runner.cleanup()
        await bot.session.close()
        await engine.dispose()
        logger.info("Бот BinzoLife штатно остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
