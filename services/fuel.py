from services.fuelprice_parser import fetch_fuelprice_prices
import logging
from database.session import AsyncSessionLocal
from database.crud import get_all_active_cities

logger = logging.getLogger(__name__)

async def refresh_prices():
    logger.info("=== refresh_prices() ВЫЗВАНА ===")
    async with AsyncSessionLocal() as db:
        cities = await get_all_active_cities(db)
        if not cities:
            logger.warning("Нет активных городов для парсинга")
            return
        for city in cities:
            try:
                await fetch_fuelprice_prices(city.name)
            except Exception as e:
                logger.error(f"Ошибка парсинга для {city.name}: {e}")
                import traceback
                logger.error(traceback.format_exc())
    logger.info("=== refresh_prices() ЗАВЕРШЕНА ===")
