import aiohttp
import asyncio
import logging
import traceback
from datetime import datetime, timezone
from database.session import AsyncSessionLocal
from database.crud import get_city_by_name, get_stations_by_city, save_price
from database.models import FuelType, SourceType

logger = logging.getLogger(__name__)

async def fetch_lukoil_prices(city_name: str = "Красноярск", retries: int = 2):
    url = "https://www.lukoil.ru/for-drivers/fuel-prices"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
    timeout = aiohttp.ClientTimeout(total=30)

    for attempt in range(retries + 1):
        try:
            logger.info(f"Попытка {attempt+1} для {city_name} (Лукойл)...")
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers, ssl=False) as resp:
                    if resp.status != 200:
                        logger.error(f"HTTP {resp.status} для {city_name}")
                        continue
                    html = await resp.text()
                    logger.info(f"Страница Лукойла загружена, размер {len(html)} байт")

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, 'lxml')
            
            # Ищем таблицу с ценами (по классам, которые часто встречаются)
            table = soup.find('table', class_='prices-table')
            if not table:
                # Пробуем найти любую таблицу с 'Аи-95'
                tables = soup.find_all('table')
                for t in tables:
                    if 'Аи-95' in t.text:
                        table = t
                        break
            if not table:
                logger.error(f"Таблица цен не найдена для {city_name}")
                return

            rows = table.find_all('tr')
            found = False
            for row in rows:
                cells = row.find_all('td')
                if len(cells) < 3:
                    continue
                city_cell = cells[0].text.strip()
                if city_name not in city_cell:
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
                        if station.brand and ("Лукойл" in station.brand or "ЛУКОЙЛ" in station.brand.upper()):
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
                    logger.info(f"Обновлено {updated} станций Лукойла в {city_name} (цена {price})")
                found = True
                break

            if not found:
                logger.warning(f"Город {city_name} не найден в таблице Лукойла")
            else:
                logger.info(f"Парсинг Лукойла для {city_name} успешен")
            return

        except Exception as e:
            logger.error(f"Попытка {attempt+1}/{retries+1} для {city_name}: {e}")
            logger.error(traceback.format_exc())
            if attempt < retries:
                await asyncio.sleep(5 * (attempt + 1))
            else:
                logger.error(f"Все попытки для {city_name} провалены")
