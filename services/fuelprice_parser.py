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
    'СКОН', 'Varta', 'Газпром нефть'
]

def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r'\b(ИНН\s*\d+|ОАО|АО|ЗАО|ООО|ООО\s*"|"|\(|\)|№|\d+)\s*', '', name, flags=re.I)
    name = re.sub(r'\s+', ' ', name).strip()
    return name.lower()

def get_brand_from_name(name: str) -> str:
    name_lower = name.lower()
    for brand in ['Лукойл', 'Газпромнефть', 'КрасноярскНП', 'Кит', 'ОПТИ', 'Роснефть', 'ТНК', 'Shell', 'BP', 'Tatneft', 'СКОН', 'Varta']:
        if brand.lower() in name_lower:
            return brand
    return None

def parse_price_from_text(text: str) -> float:
    match = re.search(r'А[иИ]-95\s*[:：]\s*([\d.,]+)', text)
    if match:
        try:
            return float(match.group(1).replace(',', '.'))
        except ValueError:
            pass
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

                # ---- 1. Пытаемся найти JavaScript-массивы (старый метод) ----
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
                            # 1. По координатам (радиус 2 км)
                            for s in stations:
                                dist = haversine_distance(lat, lon, s.latitude, s.longitude)
                                if dist < 2.0:
                                    station = s
                                    break
                            # 2. По названию
                            if not station:
                                norm_name = normalize_name(raw_name)
                                for s in stations:
                                    s_name = normalize_name(s.name)
                                    if norm_name and s_name and (norm_name in s_name or s_name in norm_name):
                                        station = s
                                        break
                            # 3. По бренду
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
                            await db.rollback()
                            continue

                    logger.info(f"Обновлено {updated_count} станций в {city_name}")
                    return

                # ---- 2. Если массивов нет — используем BeautifulSoup (новый метод) ----
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
                    if is_brand and len(text) < 100:
                        if current_name and current_price is not None:
                            station = await find_station(db, stations, current_name, current_address, None, None)
                            if station:
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
                        if 'ул' in text or 'пер' in text or 'шоссе' in text or 'просп' in text or 'пр-кт' in text:
                            current_address = text
                            continue

                    if current_name:
                        price = parse_price_from_text(text)
                        if price is not None:
                            current_price = price
                            station = await find_station(db, stations, current_name, current_address, None, None)
                            if station:
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

                if current_name and current_price is not None:
                    station = await find_station(db, stations, current_name, current_address, None, None)
                    if station:
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
