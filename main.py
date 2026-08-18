import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web

from config import settings
from database.session import engine
from handlers import start, menu, find, profile, admin, notifications, common, payments
from services.notifications import check_notifications
from services.fuel import refresh_prices

logger = logging.getLogger(__name__)

bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

dp.include_router(start.router)
dp.include_router(menu.router)
dp.include_router(find.router)
dp.include_router(profile.router)
dp.include_router(notifications.router)
dp.include_router(admin.router)
dp.include_router(payments.router)
dp.include_router(common.router)

# ---------- HTTP ----------
async def health_handler(request):
    return web.Response(text='{"status":"ok"}', content_type='application/json')

async def tasks_notifications_handler(request):
    token = request.headers.get("X-Internal-Token")
    if token != settings.INTERNAL_TOKEN:
        return web.Response(status=403, text="Forbidden")
    await check_notifications()
    return web.Response(text='{"status":"notifications_checked"}', content_type='application/json')

async def tasks_prices_handler(request):
    token = request.headers.get("X-Internal-Token")
    if token != settings.INTERNAL_TOKEN:
        return web.Response(status=403, text="Forbidden")
    await refresh_prices()
    return web.Response(text='{"status":"prices_refreshed"}', content_type='application/json')

def setup_http_server():
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post("/internal/tasks/notifications", tasks_notifications_handler)
    app.router.add_post("/internal/tasks/prices", tasks_prices_handler)
    return app

async def on_startup():
    # Создание таблиц (закомментировано, используйте Alembic или раскомментируйте для первого раза)
    # async with engine.begin() as conn:
    #     from database.models import Base
    #     await conn.run_sync(Base.metadata.create_all)
    logger.info("Бот запущен")

async def on_shutdown():
    await engine.dispose()
    logger.info("Бот остановлен")

async def main():
    app = setup_http_server()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', settings.PORT)
    await site.start()
    logger.info(f"HTTP сервер запущен на порту {settings.PORT}")

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await engine.dispose()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
