import aiohttp
import asyncio
from bs4 import BeautifulSoup
import logging
from datetime import datetime, timezone
from database.session import AsyncSessionLocal
from database.crud import get_city_by_name, get_stations_by_city, save_price
from database.models import FuelType, SourceType

logger = logging.getLogger(__name__)

async def fetch_gazprom_prices(city_name: str = "Красноярск", retries: int = 2):
    url = "https://www.gazprom-neft.ru/for-motorists/fuel-prices/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "ru-RU,ru;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive"
    }
    timeout = aiohttp.ClientTimeout(total=30)

    for attempt in range(retries + 1):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, ssl=False) as resp:
                    if resp.status != 200:
                        logger.error(f"HTTP {resp.status} для {city_name}")
                        continue
                    html = await resp.text()

            soup = BeautifulSoup(html, 'lxml')
            tables = soup.find_all('table')
            target_table = None
            for table in tables:
                if 'Аи-95' in table.text:
                    target_table = table
                    break

            if not target_table:
                logger.error(f"Таблица с ценами не найдена для {city_name}")
                return

            rows = target_table.find_all('tr')
            found = False
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 3:
                    continue
                if city_name not in cells[0].text:
                    continue
                price_text = cells[2].text.strip().replace(',', '.').replace(' ', '')
                try:
                    price = float(price_text)
                except ValueError:
                    logger.warning(f"Не удалось распарсить цену: {price_text}")
                    continue

                async with AsyncSessionLocal() as db:
                    city = await get_city_by_name(db, city_name)
                    if not city:
                        logger.warning(f"Город {city_name} не найден в БД")
                        return
                    stations = await get_stations_by_city(db, city.id)
                    updated = 0
                    for station in stations:
                        if station.brand and "Газпромнефть" in station.brand:
                            await save_price(
                                db,
                                station.id,
                                FuelType.AI_95,
                                price,
                                SourceType.PARSER,
                                confidence=0.8,
                                recorded_at=datetime.now(timezone.utc)
                            )
                            updated += 1
                    logger.info(f"Обновлено {updated} станций в {city_name} (цена {price})")
                found = True
                break

            if not found:
                logger.warning(f"Город {city_name} не найден в таблице")
            else:
                logger.info(f"Парсинг {city_name} успешен")
            return

        except Exception as e:
            logger.error(f"Попытка {attempt+1}/{retries+1} для {city_name}: {e}")
            if attempt < retries:
                await asyncio.sleep(5 * (attempt + 1))
            else:
                logger.error(f"Все попытки для {city_name} провалены")
