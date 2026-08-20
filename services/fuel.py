from services.gazprom_parser import fetch_gazprom_prices
import logging

logger = logging.getLogger(__name__)

async def refresh_prices():
    """Обновляет цены для всех указанных городов."""
    cities = ["Красноярск", "Ефремов", "Тула", "Москва", "Новомосковск"]
    for city in cities:
        try:
            await fetch_gazprom_prices(city)
            logger.info(f"Цены для города {city} обновлены")
        except Exception as e:
            logger.error(f"Ошибка парсинга для города {city}: {e}")
    logger.info("Обновление цен завершено")
