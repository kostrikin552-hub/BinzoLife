from services.fuelprice_parser import fetch_fuelprice_prices
import logging
from database.session import AsyncSessionLocal
from database.crud import get_all_active_cities, get_stations_by_city

logger = logging.getLogger(__name__)

async def refresh_prices():
    logger.info("=== refresh_prices() ВЫЗВАНА ===")
    async with AsyncSessionLocal() as db:
        cities = await get_all_active_cities(db)
        if not cities:
            logger.warning("Нет активных городов для парсинга")
            return
        for city in cities:
            # Проверяем, есть ли в городе активные станции
            stations = await get_stations_by_city(db, city.id)
            if not stations:
                logger.info(f"В городе {city.name} нет АЗС, пропускаем парсинг")
                continue
            try:
                await fetch_fuelprice_prices(city.name)
            except Exception as e:
                logger.error(f"Ошибка парсинга для {city.name}: {e}")
                import traceback
                logger.error(traceback.format_exc())
    logger.info("=== refresh_prices() ЗАВЕРШЕНА ===")
