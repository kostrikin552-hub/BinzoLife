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
    get_city_by_name, get_city_by_id, create_station, save_price, set_city_slug,
    get_or_create_city, get_stations_by_city, get_city_slug
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

# Расширенный список ключевых слов для адреса
ADDRESS_KEYWORDS = [
    'ул', 'пер', 'шоссе', 'просп', 'пр-кт', 'бульвар', 'пл',
    'пр-т', 'наб', 'набережная', 'вл', 'владение', 'д',
    'дом', 'корп', 'строение', 'проезд', 'пр-д', 'АЗС №'
]

def truncate_string(value: str, max_length: int = 255) -> str:
    if not value:
        return ""
    if len(value) > max_length:
        return value[:max_length]
    return value

def is_address(text: str) -> bool:
    """Проверяет, содержит ли текст признаки адреса."""
    if not text:
        return False
    # Проверяем на наличие ключевых слов
    for kw in ADDRESS_KEYWORDS:
        if kw in text.lower():
            return True
    # Также проверяем наличие цифр и слов "г.", "город"
    if re.search(r'(г\.|город)\s*[а-я]', text, re.I):
        return True
    return False

def clean_address(text: str) -> str:
    """Очищает адрес от лишних символов и лишних данных."""
    if not text:
        return ""
    # Убираем лишние пробелы
    text = re.sub(r'\s+', ' ', text).strip()
    # Убираем лишние запятые в конце
    text = re.sub(r',\s*$', '', text)
    return text

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
    """
    Парсит страницу и возвращает список (название, адрес, цена_АИ95).
    Улучшенная версия: ищет адрес как отдельный блок между названием и ценами.
    """
    soup = BeautifulSoup(html, 'html.parser')
    stations = []
    current_name = None
    current_address = None
    current_price = None
    # Флаг, что мы внутри блока станции после названия
    in_station_block = False

    for elem in soup.find_all(['h2', 'h3', 'strong', 'p', 'div']):
        text = elem.get_text(strip=True)
        if not text:
            continue

        # Проверяем, является ли элемент названием сети
        is_brand = any(b in text for b in BRAND_KEYWORDS)
        if is_brand and len(text) < 150:
            # Если у нас уже была станция, сохраняем её
            if current_name and current_price is not None:
                stations.append((current_name, current_address or "", current_price))
            # Начинаем новую станцию
            current_name = text
            current_address = None
            current_price = None
            in_station_block = True
            continue

        # Если мы внутри блока станции и у нас нет адреса
        if in_station_block and current_name and not current_address:
            # Проверяем, является ли этот элемент адресом
            if is_address(text):
                current_address = clean_address(text)
                continue
            # Если текст содержит "АЗС №" или "г." и не содержит цены, тоже считаем адресом
            if re.search(r'АЗС №|г\.|город|ул\.|шоссе|проезд|вл\.', text, re.I):
                current_address = clean_address(text)
                continue
            # Если текст не слишком длинный и не содержит цифр (не цена), считаем адресом
            if len(text) < 200 and not re.search(r'\d+\.?\d*\s*₽', text):
                # Если это не цена, возможно, это адрес, но проверим, что это не цена
                current_address = clean_address(text)
                continue

        # Если есть цена АИ-95
        match = re.search(r'А[иИ]-95\s*[:：]\s*([\d.,]+)', text)
        if match:
            try:
                current_price = float(match.group(1).replace(',', '.'))
                # Если у нас есть название, но нет адреса, то пытаемся извлечь адрес из ранее собранных данных
                # Но мы уже попытались выше, так что если адрес всё ещё None, оставляем пустым
                stations.append((current_name, current_address or "", current_price))
                current_name = None
                current_address = None
                current_price = None
                in_station_block = False
            except ValueError:
                pass

    # Добавляем последнюю станцию, если осталась
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

        city_id = city.id

    created = 0
    updated_prices = 0
    updated_addresses = 0

    for name, address, price in station_data:
        try:
            async with AsyncSessionLocal() as db:
                city = await get_city_by_id(db, city_id)
                if not city:
                    logger.error(f"Город {city_id} не найден")
                    continue

                existing_stations = await get_stations_by_city(db, city.id)
                existing_names = {s.name.lower(): s for s in existing_stations}
                existing_addresses = {s.address.lower(): s for s in existing_stations}

                clean_name = truncate_string(name, 255)
                clean_address = truncate_string(address, 255) if address else ""

                station = None
                norm_name = clean_name.lower()
                norm_address = clean_address.lower() if clean_address else ''

                if norm_name in existing_names:
                    station = existing_names[norm_name]
                elif norm_address and norm_address in existing_addresses:
                    station = existing_addresses[norm_address]

                if not station:
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
                    existing_names[norm_name] = station
                    if norm_address:
                        existing_addresses[norm_address] = station
                else:
                    if clean_address and station.address != clean_address:
                        station.address = clean_address
                        updated_addresses += 1

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
            logger.debug(f"IntegrityError при обработке записи: {e}")
            continue
        except Exception as e:
            logger.error(f"Ошибка при обработке записи: {e}")
            continue

    return {
        "city": city_name,
        "slug": slug,
        "stations_created": created,
        "prices_updated": updated_prices,
        "addresses_updated": updated_addresses
    }
