import logging
import re
from datetime import datetime, timezone
from typing import Dict, Any
import aiohttp
from bs4 import BeautifulSoup

from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, create_station, save_price, set_city_slug,
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
}

def truncate_string(value: str, max_length: int = 299) -> str:
    if not value:
        return ""
    if len(value) > max_length:
        return value[:max_length]
    return value

async def import_city_from_url(url: str) -> Dict[str, Any]:
    logger.info(f"Начинаем импорт города из URL: {url}")

    slug_match = re.search(r'fuelprice\.ru/([^/?]+)', url)
    if not slug_match:
        return {"error": "Неверный URL, не удалось извлечь слаг"}
    slug = slug_match.group(1)

    # 1. Определяем название города
    city_name = SLUG_TO_CITY.get(slug)
    if not city_name:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
        }
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=30) as resp:
                    if resp.status != 200:
                        return {"error": f"HTTP {resp.status}"}
                    html = await resp.text()
            except Exception as e:
                return {"error": f"Ошибка загрузки: {e}"}
        soup = BeautifulSoup(html, 'lxml')
        title_tag = soup.find('title')
        if title_tag:
            title_text = title_tag.text.strip()
            match = re.search(r'в\s+([^,]+)', title_text)
            if match:
                city_name = match.group(1).strip()
            else:
                parts = title_text.split()
                if parts:
                    last = parts[-1].replace('Цены', '').strip()
                    if last:
                        city_name = last
        if not city_name:
            city_name = slug.capitalize()

    # 2. Загружаем страницу для парсинга АЗС (повторно, но можно передать html)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, timeout=30) as resp:
                if resp.status != 200:
                    return {"error": f"HTTP {resp.status}"}
                html = await resp.text()
        except Exception as e:
            return {"error": f"Ошибка загрузки: {e}"}

    station_pattern = re.compile(
        r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*([^,]+),\s*([^,]+),\s*([^\]]+)\]'
    )
    matches = station_pattern.findall(html)
    if not matches:
        return {"error": "Не удалось найти данные АЗС на странице"}

    async with AsyncSessionLocal() as db:
        city = await get_or_create_city(db, city_name)
        if not city:
            return {"error": f"Не удалось создать город {city_name}"}

        # Проверяем существование слага и устанавливаем, если отсутствует
        try:
            existing_slug = await get_city_slug(db, city_name)
            if existing_slug:
                logger.info(f"Слаг для города {city_name} уже существует: {existing_slug}, пропускаем установку")
            else:
                await set_city_slug(db, city.id, slug)
                logger.info(f"Слаг {slug} установлен для города {city_name}")
        except Exception as e:
            logger.error(f"Ошибка при работе со слагом для {city_name}: {e}")
            await db.rollback()
            # Продолжаем импорт станций даже без слага

        existing_stations = await get_stations_by_city(db, city.id)
        existing_names = {s.name.lower(): s for s in existing_stations}
        existing_addresses = {s.address.lower(): s for s in existing_stations}

        created = 0
        updated_prices = 0

        for match in matches:
            try:
                # Каждая запись в своей транзакции, чтобы изолировать ошибки
                async with db.begin():
                    lat = float(match[0])
                    lon = float(match[1])
                    raw_name = match[2].strip()
                    address = match[3].strip()
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

                    brand = None
                    brand_keywords = ['Лукойл', 'Газпромнефть', 'КрасноярскНП', 'Кит', 'ОПТИ', 'Роснефть', 'ТНК', 'Shell', 'BP', 'Tatneft']
                    for b in brand_keywords:
                        if b.lower() in raw_name.lower():
                            brand = b
                            break

                    clean_name = truncate_string(raw_name, 299)
                    clean_address = truncate_string(address, 299)

                    station = None
                    norm_name = clean_name.lower()
                    norm_address = clean_address.lower() if clean_address else ''

                    if norm_name in existing_names:
                        station = existing_names[norm_name]
                    elif norm_address and norm_address in existing_addresses:
                        station = existing_addresses[norm_address]
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

            except Exception as e:
                # Внутри async with db.begin() ошибка уже откатит транзакцию автоматически,
                # и сессия останется чистой для следующей итерации.
                logger.error(f"Ошибка при обработке записи: {e}")
                continue

        await db.commit()

        return {
            "city": city_name,
            "slug": slug,
            "stations_created": created,
            "prices_updated": updated_prices
        }
