from services.lukoil_parser import fetch_lukoil_prices
import logging

logger = logging.getLogger(__name__)

async def refresh_prices():
    """Обновляет цены для всех указанных городов через парсер Лукойла."""
    cities = ["Красноярск", "Ефремов", "Тула", "Москва", "Новомосковск"]
    for city in cities:
        try:
            await fetch_lukoil_prices(city)
            logger.info(f"Цены Лукойла для города {city} обновлены")
        except Exception as e:
            logger.error(f"Ошибка парсинга Лукойла для {city}: {e}")
    logger.info("Обновление цен завершено")
