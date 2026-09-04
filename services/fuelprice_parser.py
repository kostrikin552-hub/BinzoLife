# services/fuelprice_parser.py — ПОЛНАЯ ВЕРСИЯ (асинхронный парсер)
import aiohttp
import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from typing import Optional, List, Tuple

from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name,
    get_all_active_stations_by_city,
    save_price,
    get_city_slug,
    set_city_slug,
)
from database.models import FuelType, SourceType
from utils.helpers import haversine_distance
from utils.cleaners import normalize_name, clean_address, get_brand_from_name, is_valid_price

logger = logging.getLogger(__name__)

# ===== Ротация User-Agent =====
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 OPR/110.0.0.0",
]

# ===== Fallback слаги =====
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
    "Ростов-на-Дону": "rostov-na-donu",
    "Уфа": "ufa",
    "Пермь": "perm",
    "Воронеж": "voronezh",
    "Волгоград": "volgograd",
    "Тула": "tula",
}

BRAND_KEYWORDS = [
    'Лукойл', 'Газпромнефть', 'КрасноярскНП', 'Кит', 'ОПТИ',
    'Роснефть', 'ТНК', 'Shell', 'BP', 'Tatneft', 'Татнефть',
    'СКОН', 'Varta'
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

# ===== АСИНХРОННАЯ ЗАГРУЗКА HTML =====
async def fetch_html_with_timeout(url: str) -> Optional[str]:
    """Асинхронно загружает HTML с жёстким таймаутом и ротацией User-Agent."""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
        "Connection": "keep-alive",
    }
    timeout = aiohttp.ClientTimeout(total=8.0, connect=3.0)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.text()
                elif resp.status == 403:
                    logger.warning(f"403 Forbidden для {url}, увеличиваем паузу")
                    await asyncio.sleep(60)
                    return None
                else:
                    logger.warning(f"HTTP {resp.status} при загрузке {url}")
                    return None
    except asyncio.TimeoutError:
        logger.error(f"Таймаут при загрузке {url}")
        return None
    except Exception as e:
        logger.error(f"Ошибка сети при загрузке {url}: {e}")
        return None

# ===== ПАРСИНГ ЦЕН ДЛЯ ОДНОГО ГОРОДА =====
async def fetch_fuelprice_prices(city_name: str = "Красноярск", retries: int = 3):
    logger.info(f"=== fetch_fuelprice_prices() для {city_name} ===")
    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, city_name)
        if not city:
            logger.warning(f"Город {city_name} не найден в БД")
            return
        stations = await get_all_active_stations_by_city(db, city.id)
        if not stations:
            logger.info(f"В городе {city_name} нет АЗС, парсинг не требуется")
            return

        slug = await get_city_slug(db, city_name)
        if not slug:
            slug = FALLBACK_SLUGS.get(city_name)
            if not slug:
                logger.warning(f"Нет слага для города {city_name}")
                return
            await set_city_slug(db, city.id, slug)
            logger.info(f"Установлен слаг {slug} для города {city_name}")

        url = f"https://fuelprice.ru/{slug}"

        for attempt in range(retries + 1):
            html = await fetch_html_with_timeout(url)
            if not html:
                if attempt < retries:
                    await asyncio.sleep(random.uniform(3, 6) * (attempt + 1))
                    continue
                else:
                    logger.error(f"Все попытки для {city_name} провалены")
                    return

            # ---- JS-массивы (основной метод) ----
            pattern = re.compile(
                r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*([^,]+),\s*([^,]+),\s*([^\]]+)\]'
            )
            matches = pattern.findall(html)
            if matches:
                logger.info(f"Найдены JS-массивы для {city_name}")
                updated_count = 0
                updates = []
                for match in matches:
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

                        station = None
                        for s in stations:
                            dist = haversine_distance(lat, lon, s.latitude, s.longitude)
                            if dist < 2.0:
                                station = s
                                break
                        if not station:
                            norm_name = normalize_name(raw_name)
                            for s in stations:
                                s_name = normalize_name(s.name)
                                if norm_name and s_name and (norm_name in s_name or s_name in norm_name):
                                    station = s
                                    break
                        if not station:
                            brand = get_brand_from_name(raw_name)
                            if brand:
                                for s in stations:
                                    if s.brand and s.brand.lower() == brand.lower():
                                        station = s
                                        break
                        if not station:
                            logger.debug(f"Не найдена станция для '{raw_name}' — пропускаем")
                            continue

                        if clean_name and station.name != clean_name:
                            station.name = clean_name
                        if clean_addr and station.address != clean_addr:
                            station.address = clean_addr
                        if lat != 0.0 and lon != 0.0:
                            station.latitude = lat
                            station.longitude = lon

                        for fuel_type, price in prices_by_fuel.items():
                            updates.append((station, fuel_type, price))
                            updated_count += 1
                            logger.info(f"Обновлено: {station.name}, {fuel_type.value} = {price} ₽")
                    except Exception as e:
                        logger.error(f"Ошибка обработки блока: {e}")
                        continue

                if updates:
                    for station, fuel_type, price in updates:
                        await save_price(db, station.id, fuel_type, price, SourceType.PARSER, confidence=0.7)
                    await db.commit()
                logger.info(f"Обновлено {updated_count} цен в {city_name}")
                return

            # ---- Fallback: BeautifulSoup ----
            logger.info("JS-массивы не найдены, используем BeautifulSoup")
            soup = BeautifulSoup(html, 'html.parser')
            updated_count = 0
            current_name = None
            current_address = None
            current_prices = {}

            for elem in soup.find_all(['h2', 'h3', 'strong', 'p', 'div']):
                text = elem.get_text(strip=True)
                if not text:
                    continue

                is_brand = any(b in text for b in BRAND_KEYWORDS)
                if is_brand and len(text) < 150:
                    if current_name and current_prices:
                        station = await find_station(db, stations, current_name, current_address, None, None)
                        if station:
                            clean_name = normalize_name(current_name)
                            clean_addr = clean_address(current_address, max_length=255) if current_address else ""
                            if clean_name and station.name != clean_name:
                                station.name = clean_name
                            if clean_addr and station.address != clean_addr:
                                station.address = clean_addr
                            for fuel_type, price in current_prices.items():
                                await save_price(db, station.id, fuel_type, price, SourceType.PARSER, confidence=0.7)
                                updated_count += 1
                    current_name = text
                    current_address = None
                    current_prices = {}
                    continue

                if current_name and not current_address:
                    if any(key in text for key in ['ул', 'пер', 'шоссе', 'просп', 'пр-кт', 'бульвар', 'пл', 'пр-т']):
                        current_address = text
                        continue

                if current_name:
                    for fuel_key, fuel_type in FUEL_TYPE_MAP.items():
                        p = re.compile(rf'{re.escape(fuel_key)}\s*[:：]\s*([\d.,]+)')
                        m = p.search(text)
                        if m:
                            try:
                                price = float(m.group(1).replace(',', '.'))
                                if is_valid_price(price):
                                    current_prices[fuel_type] = price
                            except:
                                pass

                    if current_prices:
                        station = await find_station(db, stations, current_name, current_address, None, None)
                        if station:
                            clean_name = normalize_name(current_name)
                            clean_addr = clean_address(current_address, max_length=255) if current_address else ""
                            if clean_name and station.name != clean_name:
                                station.name = clean_name
                            if clean_addr and station.address != clean_addr:
                                station.address = clean_addr
                            for fuel_type, price in current_prices.items():
                                await save_price(db, station.id, fuel_type, price, SourceType.PARSER, confidence=0.7)
                                updated_count += 1
                        current_name = None
                        current_address = None
                        current_prices = {}

            if current_name and current_prices:
                station = await find_station(db, stations, current_name, current_address, None, None)
                if station:
                    clean_name = normalize_name(current_name)
                    clean_addr = clean_address(current_address, max_length=255) if current_address else ""
                    if clean_name and station.name != clean_name:
                        station.name = clean_name
                    if clean_addr and station.address != clean_addr:
                        station.address = clean_addr
                    for fuel_type, price in current_prices.items():
                        await save_price(db, station.id, fuel_type, price, SourceType.PARSER, confidence=0.7)
                        updated_count += 1

            if updated_count:
                await db.commit()
            logger.info(f"Обновлено {updated_count} цен в {city_name} (BS4)")
            return

        # конец цикла попыток
        if attempt >= retries:
            logger.error(f"Все попытки для {city_name} провалены")

# ===== ПОИСК СТАНЦИИ =====
async def find_station(db, stations, name: str, address: str, lat: float = None, lon: float = None):
    if lat is not None and lon is not None and lat != 0.0 and lon != 0.0:
        for s in stations:
            dist = haversine_distance(lat, lon, s.latitude, s.longitude)
            if dist < 2.0:
                return s
    norm_name = normalize_name(name)
    for s in stations:
        s_name = normalize_name(s.name)
        if norm_name and s_name and (norm_name in s_name or s_name in norm_name):
            return s
    if address:
        clean_addr = clean_address(address, max_length=255)
        for s in stations:
            if s.address and clean_addr.lower() in s.address.lower():
                return s
    brand = get_brand_from_name(name)
    if brand:
        for s in stations:
            if s.brand and s.brand.lower() == brand.lower():
                return s
    return None

# ===== ПАРСИНГ ВСЕХ ГОРОДОВ =====
async def parse_all_cities():
    from database.crud import get_all_active_cities
    async with AsyncSessionLocal() as db:
        cities = await get_all_active_cities(db)
        if not cities:
            logger.warning("Нет активных городов для парсинга")
            return
        for city in cities:
            try:
                await fetch_fuelprice_prices(city.name)
                logger.info(f"Парсинг {city.name} завершён")
            except Exception as e:
                logger.error(f"Ошибка парсинга {city.name}: {e}")
            await asyncio.sleep(random.uniform(2, 5))

# ===== ФОНОВЫЙ ВОРКЕР =====
async def fuel_price_parser_worker():
    logger.info("[FuelPriceParser] Воркер запущен")
    await asyncio.sleep(30)
    while True:
        try:
            logger.info("[FuelPriceParser] Запуск цикла парсинга цен...")
            await parse_all_cities()
            logger.info("[FuelPriceParser] Цикл парсинга завершён")
        except asyncio.CancelledError:
            logger.info("[FuelPriceParser] Воркер остановлен")
            break
        except Exception as e:
            logger.error(f"[FuelPriceParser] Ошибка в цикле парсинга: {e}", exc_info=True)
        await asyncio.sleep(14400)  # 4 часа
