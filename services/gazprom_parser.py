import aiohttp
from bs4 import BeautifulSoup
import logging
from datetime import datetime, timezone
from database.session import AsyncSessionLocal
from database.crud import get_city_by_name, get_stations_by_city, save_price
from database.models import FuelType, SourceType

logger = logging.getLogger(__name__)

async def fetch_gazprom_prices(city_name: str = "Красноярск"):
    url = "https://www.gazprom-neft.ru/for-motorists/fuel-prices/"
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                logger.error(f"Ошибка загрузки: {resp.status}")
                return
            html = await resp.text()
    soup = BeautifulSoup(html, 'lxml')
    # Ищем таблицу с ценами (селекторы могут отличаться, но мы даём пример)
    table = soup.find('table', class_='price-table')
    if not table:
        logger.error("Таблица не найдена")
        return
    rows = table.find_all('tr')
    for row in rows:
        city_cell = row.find('td', class_='city')
        if not city_cell or city_name not in city_cell.text:
            continue
        price_cell = row.find('td', class_='ai-95')
        if not price_cell:
            continue
        price_text = price_cell.text.strip().replace(',', '.').replace(' ', '')
        try:
            price = float(price_text)
        except ValueError:
            continue
        async with AsyncSessionLocal() as db:
            city = await get_city_by_name(db, city_name)
            if not city:
                return
            stations = await get_stations_by_city(db, city.id)
            for station in stations:
                if station.brand and "Газпромнефть" in station.brand:
                    await save_price(db, station.id, FuelType.AI_95, price, SourceType.PARSER, confidence=0.8, recorded_at=datetime.now(timezone.utc))
    logger.info("Парсинг Газпромнефти завершён")
