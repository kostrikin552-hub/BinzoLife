import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select, and_

from database.session import AsyncSessionLocal
from database.models import Station
from database.crud import get_cached_address, cache_address
from utils.cleaners import is_likely_address, clean_address
from utils.geocoder import reverse_geocode

logger = logging.getLogger(__name__)

async def update_station_addresses(limit: int = 100):
    """
    Обновляет адреса для станций, у которых координаты не нулевые,
    но адрес не похож на реальный.
    """
    async with AsyncSessionLocal() as db:
        stations = await db.execute(
            select(Station).where(
                and_(
                    Station.latitude != 0.0,
                    Station.longitude != 0.0
                )
            )
        )
        stations = stations.scalars().all()
        updated = 0
        for station in stations:
            raw = station.address or ""
            cleaned = clean_address(raw)
            if is_likely_address(cleaned):
                continue

            # Проверяем кеш
            cached = await get_cached_address(db, station.latitude, station.longitude)
            if cached:
                station.address = cached
                updated += 1
                continue

            # Запрос к геокодеру с задержкой (защита от бана Nominatim)
            geo_addr = await reverse_geocode(station.latitude, station.longitude)
            if geo_addr:
                await cache_address(db, station.latitude, station.longitude, geo_addr)
                station.address = geo_addr
                updated += 1
                logger.info(f"Обновлён адрес для станции {station.id}: {geo_addr}")
            else:
                logger.warning(f"Не удалось получить адрес для станции {station.id}")

            # ОБЯЗАТЕЛЬНАЯ ЗАДЕРЖКА: 1.1 секунды между запросами к Nominatim
            await asyncio.sleep(1.1)

        await db.commit()
        logger.info(f"Обновлено адресов для {updated} станций")
        return updated

async def run_address_updater():
    """Запускается в фоновом режиме, раз в сутки."""
    while True:
        try:
            await update_station_addresses()
        except Exception as e:
            logger.error(f"Ошибка в address_updater: {e}")
        await asyncio.sleep(86400)  # 24 часа
