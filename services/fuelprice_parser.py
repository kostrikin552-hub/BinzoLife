# services/fuelprice_parser.py
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
    get_city_by_name,
    get_all_active_stations_by_city,
    save_price,
    get_city_slug,
    set_city_slug,
    get_or_create_city,
)
from database.models import FuelType, SourceType
from utils.helpers import haversine_distance
from utils.cleaners import normalize_name, clean_address, get_brand_from_name, is_valid_price

logger = logging.getLogger(__name__)

# Сопоставление слагов городов (fallback)
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

# Карта типов топлива для парсинга
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


async def fetch_fuelprice_prices(city_name: str = "Красноярск", retries: int = 3):
    """
    Основная функция парсинга цен для одного города.
    Скачивает страницу fuelprice.ru, извлекает цены и сохраняет в БД.
    """
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

                # ---- JS-массивы (основной метод) ----
                pattern = re.compile(
                    r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*([^,]+),\s*([^,]+),\s*([^\]]+)\]'
                )
                matches = pattern.findall(html)
                if matches:
                    logger.info(f"Найдены JS-массивы для {city_name}")
                    updated_count = 0
                    updates = []  # (station, fuel_type, price)

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

                            # Парсим цены для всех видов топлива из fuel_data
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
                                        prices_by_fuel[FuelType.AI_95] = price  # дефолт
                                except:
                                    pass

                            if not prices_by_fuel:
                                continue

                            # Поиск станции
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

                            # Обновляем поля станции
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
                            await save_price(
                                db,
                                station.id,
                                fuel_type,
                                price,
                                SourceType.PARSER,
                                confidence=0.7,
                                recorded_at=datetime.now(timezone.utc)
                            )
                        await db.commit()
                    logger.info(f"Обновлено {updated_count} цен в {city_name}")
                    return

                # ---- Fallback: BeautifulSoup ----
                logger.info(f"JS-массивы не найдены, используем BeautifulSoup")
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
                        # Сохраняем предыдущую станцию, если есть
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
                                    logger.info(f"Обновлено (BS4): {station.name}, {fuel_type.value} = {price} ₽")
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
                                    logger.info(f"Обновлено (BS4): {station.name}, {fuel_type.value} = {price} ₽")
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
                            logger.info(f"Обновлено (BS4): {station.name}, {fuel_type.value} = {price} ₽")

                if updated_count:
                    await db.commit()
                logger.info(f"Обновлено {updated_count} цен в {city_name} (BS4)")
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
    """
    Поиск станции по координатам, названию, адресу или бренду.
    """
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


async def parse_all_cities():
    """
    Функция для парсинга всех активных городов.
    Вызывается из воркера для полного цикла обновления.
    """
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
            await asyncio.sleep(1)


# ============================================================
# ФОНОВЫЙ ВОРКЕР ДЛЯ ПЕРИОДИЧЕСКОГО ЗАПУСКА ПАРСЕРА
# ============================================================
async def fuel_price_parser_worker():
    """
    Фоновый воркер, который запускает парсинг всех городов каждые 4 часа.
    Используется в main.py как отдельная задача.
    """
    logger.info("[FuelPriceParser] Воркер запущен")
    await asyncio.sleep(30)  # Даём боту время на полный старт

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

        # Интервал между полными циклами — 4 часа (14400 секунд)
        await asyncio.sleep(14400)
# Добавьте в конец services/fuelprice_parser.py:

async def fuel_price_parser_worker():
    """Фоновый воркер парсера цен."""
    logger.info("[FuelPriceParser] Воркер запущен.")
    await asyncio.sleep(20)
    while True:
        try:
            if "run_parser" in globals():
                await globals()["run_parser"]()
            elif "parse_all_cities" in globals():
                await globals()["parse_all_cities"]()
            else:
                logger.info("[FuelPriceParser] Запуск стандартного цикла обновления цен...")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[FuelPriceParser] Ошибка при парсинге: {e}")
        # Интервал 4 часа
        await asyncio.sleep(14400)
