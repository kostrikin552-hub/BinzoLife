import aiohttp
import asyncio
import logging
import traceback
import re
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, get_all_active_stations_by_city, save_price,
    get_city_slug, set_city_slug
)
from database.models import FuelType, SourceType

logger = logging.getLogger(__name__)

FALLBACK_SLUGS = {
    "Красноярск": "krasnoyarsk",
    "Москва": "moskva",
    "Санкт-Петербург": "sankt-peterburg",
    "Новосибирск": "novosibirsk",
    "Екатеринбург": "ekaterinburg",
    "Казань": "kazan",
    "Нижний Новгород": "nizhnij-novgorod",
    "Челябинск": "chelyabinsk",
    "Омск": "omsk",
    "Самара": "samara",
}

async def fetch_fuelprice_prices(city_name: str = "Красноярск", retries: int = 3):
    logger.info(f"=== fetch_fuelprice_prices() для {city_name} ===")
    async with AsyncSessionLocal() as db:
        slug = await get_city_slug(db, city_name)
        if not slug:
            slug = FALLBACK_SLUGS.get(city_name)
            if not slug:
                logger.error(f"Нет слага для города {city_name}")
                return
            city = await get_city_by_name(db, city_name)
            if city:
                await set_city_slug(db, city.id, slug)
        url = f"https://fuelprice.ru/{slug}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        }
        timeout = aiohttp.ClientTimeout(total=60)

        for attempt in range(retries + 1):
            try:
                logger.info(f"Попытка {attempt+1}/{retries+1} для {city_name}, URL: {url}")
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            logger.error(f"HTTP {resp.status} для {city_name}")
                            continue
                        html = await resp.text()
                        logger.info(f"Страница загружена, размер {len(html)} байт")

                price_pattern = re.compile(r'Аи-95\s*:\s*([\d.,]+)')
                match = price_pattern.search(html)
                if not match:
                    price_pattern = re.compile(r'АИ-95\s*:\s*([\d.,]+)')
                    match = price_pattern.search(html)
                if not match:
                    soup = BeautifulSoup(html, 'lxml')
                    price_elem = soup.find('span', class_='price')
                    if price_elem:
                        price_text = price_elem.text.strip().replace(',', '.').replace(' ', '')
                        try:
                            price = float(price_text)
                        except ValueError:
                            logger.error(f"Не удалось распарсить цену из span: {price_text}")
                            continue
                    else:
                        logger.error(f"Не найдена цена АИ-95 для {city_name}")
                        continue
                else:
                    price_text = match.group(1).replace(',', '.').replace(' ', '')
                    try:
                        price = float(price_text)
                    except ValueError:
                        logger.error(f"Не удалось распарсить цену: {price_text}")
                        continue

                logger.info(f"Найдена цена: {price}")

                async with AsyncSessionLocal() as db:
                    city = await get_city_by_name(db, city_name)
                    if not city:
                        logger.warning(f"Город {city_name} не найден в БД")
                        return
                    stations = await get_all_active_stations_by_city(db, city.id)
                    if not stations:
                        logger.warning(f"В городе {city_name} нет АЗС в БД")
                        return
                    updated = 0
                    for station in stations:
                        await save_price(
                            db,
                            station.id,
                            FuelType.AI_95,
                            price,
                            SourceType.PARSER,
                            confidence=0.7,
                            recorded_at=datetime.now(timezone.utc)
                        )
                        updated += 1
                    logger.info(f"Обновлено {updated} станций в {city_name} (цена {price})")
                return

            except asyncio.TimeoutError:
                logger.error(f"Таймаут при попытке {attempt+1}/{retries+1} для {city_name}")
            except Exception as e:
                logger.error(f"Попытка {attempt+1}/{retries+1} для {city_name} не удалась: {e}")
                logger.error(traceback.format_exc())

            if attempt < retries:
                delay = 5 * (attempt + 1)
                logger.info(f"Повтор через {delay} секунд...")
                await asyncio.sleep(delay)
            else:
                logger.error(f"Все попытки для {city_name} провалены")
