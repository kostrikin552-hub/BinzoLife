# services/fuelprice_parser.py – ИСПРАВЛЕННАЯ ВЕРСИЯ (закрытие aiohttp-сессии)

import aiohttp
import asyncio
import logging
import traceback
import re
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError
from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, get_all_active_stations_by_city, save_price,
    get_city_slug, set_city_slug
)
from database.models import FuelType, SourceType
from utils.helpers import haversine_distance
from utils.cleaners import normalize_name, clean_address, get_brand_from_name, is_valid_price

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
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        }
        timeout = aiohttp.ClientTimeout(total=60)

        for attempt in range(retries + 1):
            try:
                logger.info(f"Попытка {attempt+1}/{retries+1} для {city_name}, URL: {url}")
                # Используем async with для автоматического закрытия сессии
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            logger.error(f"HTTP {resp.status} для {city_name}")
                            continue
                        html = await resp.text()
                        logger.info(f"Страница загружена, размер {len(html)} байт")

                # Сессия закрыта, работаем с html
                # ---- JS-массивы (основной метод) ----
                pattern = re.compile(r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*([^,]+),\s*([^,]+),\s*([^\]]+)\]')
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
                            if not price or not is_valid_price(price):
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
                            updates.append((station, price))
                            updated_count += 1
                            logger.info(f"Обновлено: {station.name}, цена {price} ₽, координаты {lat},{lon}")

                        except Exception as e:
                            logger.error(f"Ошибка обработки блока: {e}")
                            continue

                    if updates:
                        for station, price in updates:
                            await save_price(
                                db,
                                station.id,
                                FuelType.AI_95,
                                price,
                                SourceType.PARSER,
                                confidence=0.7,
                                recorded_at=datetime.now(timezone.utc)
                            )
                        await db.commit()
                    logger.info(f"Обновлено {updated_count} станций в {city_name}")
                    return

                # ---- Fallback: BeautifulSoup ----
                logger.info(f"JS-массивы не найдены, используем BeautifulSoup")
                soup = BeautifulSoup(html, 'html.parser')
                updated_count = 0
                current_name = None
                current_address = None
                current_price = None

                for elem in soup.find_all(['h2', 'h3', 'strong', 'p', 'div']):
                    text = elem.get_text(strip=True)
                    if not text:
                        continue

                    is_brand = any(b in text for b in BRAND_KEYWORDS)
                    if is_brand and len(text) < 150:
                        if current_name and current_price is not None and is_valid_price(current_price):
                            station = await find_station(db, stations, current_name, current_address, None, None)
                            if station:
                                clean_name = normalize_name(current_name)
                                clean_addr = clean_address(current_address, max_length=255) if current_address else ""
                                if clean_name and station.name != clean_name:
                                    station.name = clean_name
                                if clean_addr and station.address != clean_addr:
                                    station.address = clean_addr
                                await save_price(db, station.id, FuelType.AI_95, current_price, SourceType.PARSER, confidence=0.7)
                                updated_count += 1
                                logger.info(f"Обновлено (BS4): {station.name}, цена {current_price} ₽")
                        current_name = text
                        current_address = None
                        current_price = None
                        continue

                    if current_name and not current_address:
                        if any(key in text for key in ['ул', 'пер', 'шоссе', 'просп', 'пр-кт', 'бульвар', 'пл', 'пр-т']):
                            current_address = text
                            continue

                    if current_name:
                        match = re.search(r'А[иИ]-95\s*[:：]\s*([\d.,]+)', text)
                        if match:
                            try:
                                current_price = float(match.group(1).replace(',', '.'))
                                if is_valid_price(current_price):
                                    station = await find_station(db, stations, current_name, current_address, None, None)
                                    if station:
                                        clean_name = normalize_name(current_name)
                                        clean_addr = clean_address(current_address, max_length=255) if current_address else ""
                                        if clean_name and station.name != clean_name:
                                            station.name = clean_name
                                        if clean_addr and station.address != clean_addr:
                                            station.address = clean_addr
                                        await save_price(db, station.id, FuelType.AI_95, current_price, SourceType.PARSER, confidence=0.7)
                                        updated_count += 1
                                        logger.info(f"Обновлено (BS4): {station.name}, цена {current_price} ₽")
                                current_name = None
                                current_address = None
                                current_price = None
                            except ValueError:
                                pass

                if current_name and current_price is not None and is_valid_price(current_price):
                    station = await find_station(db, stations, current_name, current_address, None, None)
                    if station:
                        clean_name = normalize_name(current_name)
                        clean_addr = clean_address(current_address, max_length=255) if current_address else ""
                        if clean_name and station.name != clean_name:
                            station.name = clean_name
                        if clean_addr and station.address != clean_addr:
                            station.address = clean_addr
                        await save_price(db, station.id, FuelType.AI_95, current_price, SourceType.PARSER, confidence=0.7)
                        updated_count += 1
                        logger.info(f"Обновлено (BS4): {station.name}, цена {current_price} ₽")

                if updated_count:
                    await db.commit()
                logger.info(f"Обновлено {updated_count} станций в {city_name} (BS4)")
                return

            except asyncio.TimeoutError:
                logger.error(f"Таймаут при попытке {attempt+1}")
            except Exception as e:
                logger.error(f"Попытка {attempt+1} не удалась: {e}")
                logger.error(traceback.format_exc())
                await db.rollback()

            if attempt < retries:
                delay = 5 * (attempt + 1)
                logger.info(f"Повтор через {delay} секунд...")
                await asyncio.sleep(delay)
            else:
                logger.error(f"Все попытки для {city_name} провалены")

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
