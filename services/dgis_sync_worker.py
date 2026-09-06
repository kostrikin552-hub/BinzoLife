# services/dgis_sync_worker.py — фоновый воркер синхронизации АЗС через 2ГИС
import asyncio
import logging
import random
from sqlalchemy import select
from database.models import City, User
from services.dgis_collector import dgis_collector
from database.crud import sync_dgis_city_stations
from utils.task_locks import task_locker

logger = logging.getLogger(__name__)


async def run_daily_dgis_sync_all_cities(session_factory):
    """Ежедневный фоновый сбор станций и адресов по городам РФ через 2ГИС"""
    if not task_locker.acquire("daily_dgis_sync", timeout_seconds=3600):
        logger.warning("Синхронизация 2ГИС уже выполняется, пропуск.")
        return

    try:
        logger.info("🚀 Запуск плановой синхронизации АЗС РФ через 2ГИС...")
        async with session_factory() as session:
            # Сначала города, где есть живые водители
            active_city_ids = set(
                (await session.execute(
                    select(User.city_id).where(User.city_id.isnot(None)).distinct()
                )).scalars().all()
            )
            all_cities = (await session.execute(
                select(City).where(City.is_active == True)
            )).scalars().all()

            sorted_cities = sorted(all_cities, key=lambda c: 0 if c.id in active_city_ids else 1)

        total_stations = 0
        for idx, city in enumerate(sorted_cities, start=1):
            if not (city.latitude and city.longitude):
                logger.warning(f"[{idx}/{len(sorted_cities)}] Город {city.name} без координат, пропускаем")
                continue

            logger.info(f"[{idx}/{len(sorted_cities)}] 2ГИС синхронизация: г. {city.name}...")
            stations = await dgis_collector.fetch_city_stations(city.name, city.latitude, city.longitude)
            if stations:
                async with session_factory() as session:
                    added, updated = await sync_dgis_city_stations(session, city.id, stations)
                    total_stations += len(stations)
                    logger.info(f"г. {city.name}: добавлено {added}, обновлено {updated} АЗС")
            else:
                logger.warning(f"Не удалось получить станции для {city.name}")

            # Вежливая пауза 3-4 сек
            await asyncio.sleep(random.uniform(3.0, 4.0))

        logger.info(f"🎉 Синхронизация 2ГИС завершена! Обработано станций: {total_stations}")

    finally:
        task_locker.release("daily_dgis_sync")


async def fuel_price_parser_worker():
    """Фоновый воркер для запуска в main.py (сохранённое имя для обратной совместимости)"""
    from database.session import AsyncSessionLocal
    logger.info("[2GIS FuelCollector] Воркер запущен.")
    # Первый запуск через 45 сек после старта
    await asyncio.sleep(45)
    while True:
        try:
            await run_daily_dgis_sync_all_cities(AsyncSessionLocal)
            # Запуск раз в 24 часа
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[2GIS FuelCollector] Ошибка: {e}", exc_info=True)
            await asyncio.sleep(300)
