# services/city_importer.py — ПОЛНАЯ ВЕРСИЯ (с поддержкой кодировок)
import logging
import re
import asyncio
import csv
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
import aiohttp
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, get_city_by_id, create_station, save_price, set_city_slug,
    get_or_create_city, get_stations_by_city, get_city_slug,
    commit_or_rollback
)
from database.models import FuelType, SourceType, FuelPrice, Station, City
from utils.cleaners import normalize_name, clean_address, get_brand_from_name, is_valid_price

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

ADDRESS_KEYWORDS = [
    'ул', 'пер', 'шоссе', 'просп', 'пр-кт', 'бульвар', 'пл',
    'пр-т', 'наб', 'набережная', 'вл', 'владение', 'д',
    'дом', 'корп', 'строение', 'проезд', 'пр-д', 'АЗС №'
]

FUEL_TYPE_MAP = {
    "Аи-92": FuelType.AI_92,
    "АИ-92": FuelType.AI_92,
    "Аи-95": FuelType.AI_95,
    "АИ-95": FuelType.AI_95,
    "Аи-98": FuelType.AI_98,
    "АИ-98": FuelType.AI_98,
    "Аи-100": FuelType.AI_100,
    "АИ-100": FuelType.AI_100,
    "ДТ": FuelType.DT,
    "ДТ-З": FuelType.DT,
    "ДТ-Е": FuelType.DT,
}


def is_address(text: str) -> bool:
    if not text:
        return False
    text = re.sub(r'<[^>]+>', '', text)
    for kw in ADDRESS_KEYWORDS:
        if kw in text.lower():
            return True
    if re.search(r'(г\.|город)\s*[а-я]', text, re.I):
        return True
    return False


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
    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    else:
                        logger.warning(f"HTTP {resp.status} при попытке {attempt+1} для {url}")
        except asyncio.TimeoutError:
            logger.warning(f"Таймаут при загрузке {url} (попытка {attempt+1})")
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


def parse_stations_from_html(html: str) -> List[Tuple[str, str, Dict[FuelType, float], float, float]]:
    soup = BeautifulSoup(html, 'html.parser')
    stations = []
    current_name = None
    current_address = None
    current_prices = {}
    current_lat = None
    current_lon = None

    # Основной метод: JS-массивы
    js_pattern = re.compile(r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*([^,]+),\s*([^,]+),\s*([^\]]+)\]')
    js_matches = js_pattern.findall(html)
    if js_matches:
        logger.info(f"Найдено {len(js_matches)} JS-записей")
        for match in js_matches:
            try:
                lat = float(match[0])
                lon = float(match[1])
                raw_name = match[2].strip()
                raw_address = match[3].strip()
                fuel_data = match[4] if len(match) > 4 else ''
                price_str = match[5] if len(match) > 5 else ''

                clean_name = normalize_name(raw_name)
                clean_addr = clean_address(raw_address, max_length=255)

                prices_by_fuel = {}
                if fuel_data:
                    for fuel_key, fuel_type in FUEL_TYPE_MAP.items():
                        p = re.compile(rf'{re.escape(fuel_key)}\s*[:：]\s*([\d.,]+)')
                        m = p.search(fuel_data)
                        if m:
                            try:
                                price = float(m.group(1).replace(',', '.'))
                                if is_valid_price(price):
                                    prices_by_fuel[fuel_type] = price
                            except:
                                pass
                if not prices_by_fuel and price_str:
                    try:
                        price = float(price_str.replace(',', '.'))
                        if is_valid_price(price):
                            prices_by_fuel[FuelType.AI_95] = price
                    except:
                        pass

                if not prices_by_fuel:
                    continue

                stations.append((clean_name or raw_name, clean_addr or raw_address, prices_by_fuel, lat, lon))
            except Exception as e:
                logger.warning(f"Ошибка обработки JS-записи: {e}")
        if stations:
            return stations

    # Fallback: BeautifulSoup
    logger.info("JS-массивы не дали результатов, пробуем BeautifulSoup")
    for elem in soup.find_all(['h2', 'h3', 'strong', 'p', 'div']):
        text = elem.get_text(strip=True)
        if not text:
            continue

        clean_text = re.sub(r'<[^>]+>', '', text)

        is_brand = any(b in clean_text for b in BRAND_KEYWORDS)
        if is_brand and len(clean_text) < 150:
            if current_name and current_prices:
                stations.append((current_name, current_address or "", current_prices, 0.0, 0.0))
            current_name = clean_text
            current_address = None
            current_prices = {}
            continue

        if current_name and not current_address:
            if is_address(clean_text):
                current_address = clean_text
                continue

        if current_name:
            for fuel_key, fuel_type in FUEL_TYPE_MAP.items():
                p = re.compile(rf'{re.escape(fuel_key)}\s*[:：]\s*([\d.,]+)')
                m = p.search(clean_text)
                if m:
                    try:
                        price = float(m.group(1).replace(',', '.'))
                        if is_valid_price(price):
                            current_prices[fuel_type] = price
                    except:
                        pass

            if current_prices:
                stations.append((current_name, current_address or "", current_prices, 0.0, 0.0))
                current_name = None
                current_address = None
                current_prices = {}

    if current_name and current_prices:
        stations.append((current_name, current_address or "", current_prices, 0.0, 0.0))

    return stations


# ========== ПОДДЕРЖКА КОДИРОВОК CSV ==========
def parse_csv_file(file_path: str) -> List[Dict]:
    """Читает CSV с автоопределением кодировки."""
    encodings = ["utf-8-sig", "utf-8", "cp1251"]
    for enc in encodings:
        try:
            with open(file_path, "r", encoding=enc) as f:
                reader = csv.DictReader(f)
                return list(reader)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise ValueError(f"Не удалось прочитать файл {file_path}. Сохраните таблицу в кодировке UTF-8.")


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

    async with AsyncSessionLocal() as db:
        try:
            await db.begin()

            city = await get_or_create_city(db, city_name)
            if not city:
                await db.rollback()
                return {"error": f"Не удалось создать город {city_name}"}

            existing_slug = await get_city_slug(db, city_name)
            if not existing_slug:
                try:
                    await set_city_slug(db, city.id, slug)
                    logger.info(f"Слаг {slug} установлен для города {city_name}")
                except IntegrityError:
                    await db.rollback()
                    alt_slug = f"{slug}_{city.id}"
                    try:
                        await set_city_slug(db, city.id, alt_slug)
                        logger.info(f"Слаг {alt_slug} установлен для города {city_name}")
                    except IntegrityError:
                        logger.error(f"Не удалось установить слаг для {city_name}")
                        await db.rollback()
                        return {"error": f"Не удалось установить слаг для {city_name}"}
                except Exception as e:
                    await db.rollback()
                    return {"error": f"Ошибка установки слага: {e}"}

            city_id = city.id

            existing_stations = await get_stations_by_city(db, city_id)
            existing_by_coords = {}
            for s in existing_stations:
                if s.latitude and s.longitude:
                    existing_by_coords[(round(s.latitude, 6), round(s.longitude, 6))] = s

            created = 0
            updated_prices = 0
            updated_addresses = 0
            updated_coords = 0

            for name, address, prices_by_fuel, lat, lon in station_data:
                try:
                    if not prices_by_fuel:
                        continue

                    clean_name = normalize_name(name)
                    clean_name = truncate_string(clean_name, 255)
                    clean_addr = clean_address(address, max_length=255) if address else ""

                    station = None
                    if lat != 0.0 and lon != 0.0:
                        key = (round(lat, 6), round(lon, 6))
                        station = existing_by_coords.get(key)

                    if not station and clean_addr:
                        for s in existing_stations:
                            if s.address and clean_addr.lower() in s.address.lower():
                                station = s
                                break

                    if not station:
                        brand = get_brand_from_name(clean_name)
                        station = Station(
                            city_id=city_id,
                            name=clean_name,
                            address=clean_addr,
                            latitude=lat if lat != 0.0 else 0.0,
                            longitude=lon if lon != 0.0 else 0.0,
                            brand=brand,
                            is_active=True
                        )
                        db.add(station)
                        await db.flush()
                        created += 1
                        if station.latitude and station.longitude:
                            existing_by_coords[(round(station.latitude, 6), round(station.longitude, 6))] = station
                    else:
                        if clean_name and station.name != clean_name:
                            station.name = clean_name
                        if clean_addr and station.address != clean_addr:
                            station.address = clean_addr
                            updated_addresses += 1
                        if lat != 0.0 and lon != 0.0 and (station.latitude != lat or station.longitude != lon):
                            station.latitude = lat
                            station.longitude = lon
                            updated_coords += 1

                    for fuel_type, price in prices_by_fuel.items():
                        price_entry = FuelPrice(
                            station_id=station.id,
                            fuel_type=fuel_type,
                            price=price,
                            source=SourceType.PARSER,
                            confidence=0.7,
                            recorded_at=datetime.now(timezone.utc),
                            is_fresh=True
                        )
                        db.add(price_entry)
                        updated_prices += 1

                except Exception as e:
                    logger.error(f"Ошибка обработки записи {name}: {e}")
                    await db.rollback()
                    return {"error": f"Ошибка при обработке записи {name}: {e}"}

            await commit_or_rollback(db)

            return {
                "city": city_name,
                "slug": slug,
                "stations_created": created,
                "prices_updated": updated_prices,
                "addresses_updated": updated_addresses,
                "coords_updated": updated_coords
            }

        except Exception as e:
            logger.error(f"Критическая ошибка при импорте {city_name}: {e}")
            await db.rollback()
            return {"error": str(e)}
