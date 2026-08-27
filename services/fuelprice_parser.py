# services/fuelprice_parser.py – ИСПРАВЛЕНА (используется normalize_name для названия)

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

def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r'\bАи-9[258]\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bАИ-9[258]\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bДТ\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bАи-100\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\d{4}-\d{2}-\d{2}', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def get_brand_from_name(name: str) -> str:
    name_lower = name.lower()
    for brand in ['Лукойл', 'Газпромнефть', 'КрасноярскНП', 'Кит', 'ОПТИ', 'Роснефть', 'ТНК', 'Shell', 'BP', 'Tatneft', 'СКОН', 'Varta']:
        if brand.lower() in name_lower:
            return brand
    return None

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
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status != 200:
                            logger.error(f"HTTP {resp.status} для {city_name}")
                            continue
                        html = await resp.text()
                        logger.info(f"Страница загружена, размер {len(html)} байт")

                # ---- Сначала пытаемся найти JS-массивы (для Красноярска) ----
                pattern = re.compile(r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*([^,]+),\s*([^,]+),\s*([^\]]+)\]')
                matches = pattern.findall(html)
                if matches:
                    logger.info(f"Найдены JS-массивы для {city_name}, используем старый метод")
                    updated_count = 0
                    for match in matches:
                        try:
                            lat = float(match[0])
                            lon = float(match[1])
                            raw_name = match[2].strip()
                            raw_address = match[3].strip()
                            if '<' in raw_address or '>' in raw_address or len(raw_address) > 200:
                                raw_address = None
                            fuel_data = match[4] if len(match) > 4 else ''
                            price_str = match[5] if len(match) > 5 else ''

                            # Очищаем название от цен и дат
                            clean_name = normalize_name(raw_name)

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
                                logger.debug(f"Не найдена станция для '{raw_name}' (коорд. {lat},{lon}) — пропускаем")
                                continue

                            # Обновляем название и адрес, если они изменились
                            if clean_name and station.name != clean_name:
                                station.name = clean_name
                            if raw_address and station.address != raw_address:
                                station.address = raw_address
                            await db.commit()

                            await save_price(
                                db,
                                station.id,
                                FuelType.AI_95,
                                price,
                                SourceType.PARSER,
                                confidence=0.7,
                                recorded_at=datetime.now(timezone.utc)
                            )
                            updated_count += 1
                            logger.info(f"Обновлена цена для {station.name}: {price} ₽")

                        except Exception as e:
                            logger.error(f"Ошибка обработки блока: {e}")
                            continue
                    logger.info(f"Обновлено {updated_count} станций в {city_name}")
                    return

                # ---- Если массивов нет — парсим HTML (BeautifulSoup) ----
                logger.info(f"JS-массивы не найдены для {city_name}, используем BeautifulSoup")
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
                        if current_name and current_price is not None:
                            station = await find_station(db, stations, current_name, current_address, None, None)
                            if station:
                                if current_address and station.address != current_address:
                                    station.address = current_address
                                clean_name = normalize_name(current_name)
                                if clean_name and station.name != clean_name:
                                    station.name = clean_name
                                await db.commit()
                                await save_price(
                                    db,
                                    station.id,
                                    FuelType.AI_95,
                                    current_price,
                                    SourceType.PARSER,
                                    confidence=0.7,
                                    recorded_at=datetime.now(timezone.utc)
                                )
                                updated_count += 1
                                logger.info(f"Обновлена цена для {station.name}: {current_price} ₽")
                        current_name = text
                        current_address = None
                        current_price = None
                        continue

                    if current_name and not current_address:
                        if any(key in text for key in ['ул', 'пер', 'шоссе', 'просп', 'пр-кт', 'бульвар', 'пл', 'пр-т']):
                            clean_addr = re.sub(r'\s+', ' ', text).strip()
                            if len(clean_addr) < 200:
                                current_address = clean_addr
                            continue

                    if current_name:
                        match = re.search(r'А[иИ]-95\s*[:：]\s*([\d.,]+)', text)
                        if match:
                            try:
                                current_price = float(match.group(1).replace(',', '.'))
                                station = await find_station(db, stations, current_name, current_address, None, None)
                                if station:
                                    if current_address and station.address != current_address:
                                        station.address = current_address
                                    clean_name = normalize_name(current_name)
                                    if clean_name and station.name != clean_name:
                                        station.name = clean_name
                                    await db.commit()
                                    await save_price(
                                        db,
                                        station.id,
                                        FuelType.AI_95,
                                        current_price,
                                        SourceType.PARSER,
                                        confidence=0.7,
                                        recorded_at=datetime.now(timezone.utc)
                                    )
                                    updated_count += 1
                                    logger.info(f"Обновлена цена для {station.name}: {current_price} ₽")
                                current_name = None
                                current_address = None
                                current_price = None
                            except ValueError:
                                pass

                if current_name and current_price is not None:
                    station = await find_station(db, stations, current_name, current_address, None, None)
                    if station:
                        if current_address and station.address != current_address:
                            station.address = current_address
                        clean_name = normalize_name(current_name)
                        if clean_name and station.name != clean_name:
                            station.name = clean_name
                        await db.commit()
                        await save_price(
                            db,
                            station.id,
                            FuelType.AI_95,
                            current_price,
                            SourceType.PARSER,
                            confidence=0.7,
                            recorded_at=datetime.now(timezone.utc)
                        )
                        updated_count += 1
                        logger.info(f"Обновлена цена для {station.name}: {current_price} ₽")

                logger.info(f"Обновлено {updated_count} станций в {city_name}")
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

async def find_station(db, stations, name: str, address: str, lat: float = None, lon: float = None):
    if lat is not None and lon is not None:
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
        for s in stations:
            if s.address and address.lower() in s.address.lower():
                return s

    brand = get_brand_from_name(name)
    if brand:
        for s in stations:
            if s.brand and s.brand.lower() == brand.lower():
                return s

    return None
