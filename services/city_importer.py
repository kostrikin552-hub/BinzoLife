import logging
import re
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional
import aiohttp
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError

from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, create_station, save_price, set_city_slug,
    get_or_create_city, get_stations_by_city, get_city_slug
)
from database.models import FuelType, SourceType

logger = logging.getLogger(__name__)

# Словарь для быстрого преобразования слагов в названия
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
}

# Альтернативные слаги для некоторых городов (если вдруг изменится URL)
ALT_SLUGS = {
    "sankt-peterburg": "spb",
    "rostov-na-donu": "rostov",
}

def normalize_city_name(raw: str) -> str:
    if not raw:
        return ""
    raw = re.sub(r'^Цены\s+на\s+топливо\s+в\s+', '', raw, flags=re.I)
    raw = re.sub(r'\s+—\s+.*$', '', raw)
    raw = re.sub(r'\s+—\s+.*', '', raw)
    raw = re.sub(r'\s+\([^)]*\)', '', raw)
    raw = raw.strip()
    return raw

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
                    return normalize_city_name(candidate)
    meta_tag = soup.find('meta', {'name': 'description'})
    if meta_tag and meta_tag.get('content'):
        content = meta_tag.get('content')
        patterns = [
            r'в\s+([^,]+)',
            r'в\s+([^\.]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                candidate = match.group(1).strip()
                if candidate and len(candidate) < 50:
                    return normalize_city_name(candidate)
    return slug.replace('-', ' ').title()

def parse_station_data(html: str) -> list:
    patterns = [
        r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*([^,]+),\s*([^,]+),\s*([^\]]+)\]',
        r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\'\]',
    ]
    for pattern in patterns:
        compiled = re.compile(pattern)
        found = compiled.findall(html)
        if found:
            return found
    return []

async def import_city_from_url(url: str) -> Dict[str, Any]:
    logger.info(f"Начинаем импорт города из URL: {url}")

    slug_match = re.search(r'fuelprice\.ru/([^/?]+)', url)
    if not slug_match:
        return {"error": "Неверный URL, не удалось извлечь слаг"}
    slug = slug_match.group(1)

    html = await fetch_html_with_retry(url)
    if not html:
        return {"error": "Не удалось загрузить страницу после нескольких попыток"}

    city_name = SLUG_TO_CITY.get(slug)
    if not city_name:
        city_name = extract_city_name_from_html(html, slug)

    matches = parse_station_data(html)
    if not matches:
        return {"error": "Не удалось найти данные АЗС на странице"}

    async with AsyncSessionLocal() as db:
        # ---- Шаг 1: Создаём город и устанавливаем слаг ----
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
                        alt_slug = ALT_SLUGS.get(slug, f"{slug}_{city.id}")
                        try:
                            await set_city_slug(db, city.id, alt_slug)
                            logger.info(f"Слаг {alt_slug} установлен для города {city_name} (альтернативный)")
                        except IntegrityError:
                            logger.error(f"Не удалось установить слаг для {city_name}")
            else:
                logger.info(f"Слаг для города {city_name} уже существует: {existing_slug}")

        # ---- Шаг 2: Получаем существующие станции ----
        existing_stations = await get_stations_by_city(db, city.id)
        existing_names = {s.name.lower(): s for s in existing_stations}
        existing_addresses = {s.address.lower(): s for s in existing_stations}

        created = 0
        updated_prices = 0
        updated_stations = 0

        # ---- Шаг 3: Импортируем станции с использованием savepoints ----
        for match in matches:
            try:
                async with db.begin_nested():
                    if len(match) >= 9:
                        lat, lon, raw_name, address, fuel_data, price_str, *_ = match
                    else:
                        lat, lon, raw_name, address, fuel_data, price_str = match
                    lat = float(lat)
                    lon = float(lon)
                    raw_name = raw_name.strip()
                    address = address.strip()
                    fuel_data = fuel_data if isinstance(fuel_data, str) else ''
                    price_str = price_str if isinstance(price_str, str) else ''

                    price = None
                    if 'Аи-95' in fuel_data or 'АИ-95' in fuel_data:
                        p_match = re.search(r'А[иИ]-95\s*[:：]\s*([\d.,]+)', fuel_data)
                        if p_match:
                            price = float(p_match.group(1).replace(',', '.'))
                    if not price and price_str:
                        try:
                            price = float(price_str.replace(',', '.'))
                        except:
                            pass
                    if not price:
                        continue

                    brand = None
                    brand_keywords = ['Лукойл', 'Газпромнефть', 'КрасноярскНП', 'Кит', 'ОПТИ', 'Роснефть', 'ТНК', 'Shell', 'BP', 'Tatneft']
                    for b in brand_keywords:
                        if b.lower() in raw_name.lower():
                            brand = b
                            break

                    clean_name = truncate_string(raw_name, 255)
                    clean_address = truncate_string(address, 255)

                    station = None
                    norm_name = clean_name.lower()
                    norm_address = clean_address.lower() if clean_address else ''

                    if norm_name in existing_names:
                        station = existing_names[norm_name]
                    elif norm_address and norm_address in existing_addresses:
                        station = existing_addresses[norm_address]

                    if station:
                        need_update = False
                        if abs(station.latitude - lat) > 0.0001 or abs(station.longitude - lon) > 0.0001:
                            station.latitude = lat
                            station.longitude = lon
                            need_update = True
                        if station.brand != brand:
                            station.brand = brand
                            need_update = True
                        if need_update:
                            updated_stations += 1
                    else:
                        station = await create_station(
                            db,
                            city_id=city.id,
                            name=clean_name,
                            address=clean_address,
                            lat=lat,
                            lon=lon,
                            brand=brand
                        )
                        created += 1
                        existing_names[norm_name] = station
                        if norm_address:
                            existing_addresses[norm_address] = station

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

            except IntegrityError as e:
                # Дубликат цены или другой конфликт целостности — просто логируем и пропускаем
                logger.debug(f"IntegrityError при обработке записи (вероятно дубликат): {e}")
                continue
            except Exception as e:
                logger.error(f"Ошибка при обработке записи: {e}")
                continue

        # ---- Шаг 4: Фиксируем внешнюю транзакцию ----
        await db.commit()

        return {
            "city": city_name,
            "slug": slug,
            "stations_created": created,
            "stations_updated": updated_stations,
            "prices_updated": updated_prices
        }
