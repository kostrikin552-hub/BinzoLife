import logging
import re
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
import aiohttp
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError

from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, create_station, save_price, set_city_slug,
    get_or_create_city, get_stations_by_city, get_city_slug, update_user
)
from database.models import FuelType, SourceType

logger = logging.getLogger(__name__)

SLUG_TO_CITY = {
    "moskva": "Москва",
    "spb": "Санкт-Петербург",
    "novosibirsk": "Новосибирск",
    "ekaterinburg": "Екатеринбург",
    "kazan": "Казань",
    "nizhniy-novgorod": "Нижний Новгород",
    "chelyabinsk": "Челябинск",
    "omsk": "Омск",
    "samara": "Самара",
    "rostov-na-donu": "Ростов-на-Дону",
    "ufa": "Уфа",
    "perm": "Пермь",
    "voronezh": "Воронеж",
    "volgograd": "Волгоград",
    "sankt-peterburg": "Санкт-Петербург",
    "krasnoyarsk": "Красноярск",
    "tula": "Тула",
}

BRAND_KEYWORDS = [
    'Лукойл', 'Газпромнефть', 'КрасноярскНП', 'Кит', 'ОПТИ',
    'Роснефть', 'ТНК', 'Shell', 'BP', 'Tatneft', 'Татнефть',
    'СКОН', 'Varta'
]

def truncate_string(value: str, max_length: int = 255) -> str:
    if not value:
        return ""
    if len(value) > max_length:
        return value[:max_length]
    return value

async def fetch_html_with_retry(url: str, retries: int = 3, delay: float = 2.0) -> Optional[str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    }
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=30) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    else:
                        logger.warning(f"HTTP {resp.status} при попытке {attempt+1} для {url}")
        except Exception as e:
            logger.warning(f"Ошибка загрузки {url} (попытка {attempt+1}): {e}")
        await asyncio.sleep(delay * (attempt + 1))
    return None

def extract_city_name_from_html(html: str, slug: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    title_tag = soup.find('title')
    if title_tag:
        title_text = title_tag.text.strip()
        patterns = [
            r'в\s+([^,]+)',
            r'^([^—]+)',
            r'^([^–]+)',
            r'^([^|]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, title_text)
            if match:
                candidate = match.group(1).strip()
                if candidate and len(candidate) < 50:
                    return candidate.strip()
    return SLUG_TO_CITY.get(slug, slug.replace('-', ' ').title())

def parse_stations_from_html(html: str) -> List[Tuple[str, str, float]]:
    soup = BeautifulSoup(html, 'html.parser')
    stations = []
    current_name = None
    current_address = None
    current_price = None

    for elem in soup.find_all(['h2', 'h3', 'strong', 'p', 'div']):
        text = elem.get_text(strip=True)
        if not text:
            continue

        is_brand = any(b in text for b in BRAND_KEYWORDS)
        if is_brand and len(text) < 100:
            if current_name and current_price is not None:
                stations.append((current_name, current_address or "", current_price))
            current_name = text
            current_address = None
            current_price = None
            continue

        if current_name and not current_address:
            if 'ул' in text or 'пер' in text or 'шоссе' in text or 'просп' in text or 'пр-кт' in text:
                current_address = text
                continue

        if current_name:
            match = re.search(r'А[иИ]-95\s*[:：]\s*([\d.,]+)', text)
            if match:
                try:
                    current_price = float(match.group(1).replace(',', '.'))
                    stations.append((current_name, current_address or "", current_price))
                    current_name = None
                    current_address = None
                    current_price = None
                except ValueError:
                    pass

    if current_name and current_price is not None:
        stations.append((current_name, current_address or "", current_price))

    return stations

async def import_city_from_url(url: str) -> Dict[str, Any]:
    logger.info(f"Начинаем импорт города из URL: {url}")

    slug_match = re.search(r'fuelprice\.ru/([^/?]+)', url)
    if not slug_match:
        return {"error": "Неверный URL, не удалось извлечь слаг"}
    slug = slug_match.group(1)

    html = await fetch_html_with_retry(url)
    if not html:
        return {"error": "Не удалось загрузить страницу после нескольких попыток"}

    city_name = extract_city_name_from_html(html, slug)
    station_data = parse_stations_from_html(html)
    if not station_data:
        return {"error": "Не удалось найти АЗС на странице"}

    # ---- Шаг 1: Создаём город и устанавливаем слаг в отдельной сессии ----
    async with AsyncSessionLocal() as db:
        async with db.begin():
            city = await get_or_create_city(db, city_name)
            if not city:
                return {"error": f"Не удалось создать город {city_name}"}

            existing_slug = await get_city_slug(db, city_name)
            if not existing_slug:
                try:
                    await set_city_slug(db, city.id, slug)
                    logger.info(f"Слаг {slug} установлен для города {city_name}")
                except IntegrityError:
                    await db.rollback()
                    existing_slug = await get_city_slug(db, city_name)
                    if not existing_slug:
                        alt_slug = f"{slug}_{city.id}"
                        try:
                            await set_city_slug(db, city.id, alt_slug)
                            logger.info(f"Слаг {alt_slug} установлен для города {city_name} (альтернативный)")
                        except IntegrityError:
                            logger.error(f"Не удалось установить слаг для {city_name}")
            else:
                logger.info(f"Слаг для города {city_name} уже существует: {existing_slug}")

    # ---- Шаг 2: Импортируем станции (каждая в своей сессии) ----
    created = 0
    updated_prices = 0
    city_id = city.id

    for name, address, price in station_data:
        try:
            async with AsyncSessionLocal() as db:
                # Получаем город и станции в этой сессии
                city = await get_city_by_id(db, city_id)
                if not city:
                    logger.error(f"Город {city_id} не найден")
                    continue

                existing_stations = await get_stations_by_city(db, city.id)
                existing_names = {s.name.lower(): s for s in existing_stations}
                existing_addresses = {s.address.lower(): s for s in existing_stations}

                clean_name = truncate_string(name, 255)
                clean_address = truncate_string(address, 255)

                station = None
                norm_name = clean_name.lower()
                norm_address = clean_address.lower() if clean_address else ''

                if norm_name in existing_names:
                    station = existing_names[norm_name]
                elif norm_address and norm_address in existing_addresses:
                    station = existing_addresses[norm_address]

                if not station:
                    # Создаём новую станцию с нулевыми координатами
                    station = await create_station(
                        db,
                        city_id=city.id,
                        name=clean_name,
                        address=clean_address,
                        lat=0.0,
                        lon=0.0,
                        brand=None
                    )
                    created += 1

                # Сохраняем цену
                await save_price(
                    db,
                    station.id,
                    FuelType.AI_95,
                    price,
                    SourceType.PARSER,
                    confidence=0.7,
                    recorded_at=datetime.now(timezone.utc)
                )
                updated_prices += 1
                # Коммит внутри сессии произойдёт автоматически при выходе из блока async with

        except IntegrityError as e:
            logger.debug(f"IntegrityError при обработке записи: {e}")
            continue
        except Exception as e:
            logger.error(f"Ошибка при обработке записи: {e}")
            continue

    return {
        "city": city_name,
        "slug": slug,
        "stations_created": created,
        "prices_updated": updated_prices
    }
