from services.fuelprice_parser import fetch_fuelprice_prices
import logging

logger = logging.getLogger(__name__)

async def refresh_prices():
    logger.info(">>> refresh_prices() вызвана")
    city = "Красноярск"
    try:
        logger.info(f"Начинаем парсинг для города {city}")
        await fetch_fuelprice_prices(city)
        logger.info(f"Цены для города {city} обновлены")
    except Exception as e:
        logger.error(f"Ошибка парсинга для {city}: {e}")
    logger.info("<<< refresh_prices() завершена")
