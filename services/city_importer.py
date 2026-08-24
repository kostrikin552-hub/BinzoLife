import logging
import re
from datetime import datetime, timezone
from typing import List, Dict, Any
import aiohttp
from bs4 import BeautifulSoup

from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, create_station, save_price, set_city_slug,
    get_or_create_city
)
from database.models import FuelType, SourceType
from utils.helpers import haversine_distance

logger = logging.getLogger(__name__)

async def import_city_from_url(url: str) -> Dict[str, Any]:
    """
    Парсит страницу fuelprice.ru, создаёт город и все АЗС с ценами.
    Возвращает словарь со статистикой.
    """
    logger.info(f"Начинаем импорт города из URL: {url}")

    # Извлекаем слаг из URL
    slug_match = re.search(r'fuelprice\.ru/([^/?]+)', url)
    if not slug_match:
        return {"error": "Неверный URL, не удалось извлечь слаг"}
    slug = slug_match.group(1)

    # Загружаем страницу
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

    # Парсинг HTML
    soup = BeautifulSoup(html, 'lxml')

    # 1. Определяем название города (из заголовка или мета-тега)
    city_name = None
    title_tag = soup.find('title')
    if title_tag:
        # Например: "Цены на топливо в Москве" -> "Москва"
        title_text = title_tag.text.strip()
        # Ищем по шаблону "в ..." или берём последнее слово
        match = re.search(r'в\s+([^,]+)', title_text)
        if match:
            city_name = match.group(1).strip()
        else:
            # fallback: берём последнее слово до запятой
            parts = title_text.split()
            if parts:
                city_name = parts[-1].replace('Цены', '').strip()
    if not city_name:
        # Если не удалось, используем слаг с заглавной буквы
        city_name = slug.capitalize()

    # 2. Ищем все блоки АЗС (используем структуру страницы)
    # Находим все элементы, содержащие координаты и цены
    # Обычно данные закодированы в script тегах или в HTML-атрибутах.
    # Попробуем найти pattern координат и названий.
    station_pattern = re.compile(
        r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*([^,]+),\s*([^,]+),\s*([^\]]+)\]'
    )
    matches = station_pattern.findall(html)

    if not matches:
        # Fallback: ищем по блокам с ценами
        # Найдём все div с классами station или item
        blocks = soup.find_all('div', class_=re.compile(r'station|item|card'))
        for block in blocks:
            # Пытаемся извлечь координаты из data-атрибутов или скрытых полей
            # Это сложно, поэтому лучше использовать JSON из скриптов.
            pass
        if not matches:
            return {"error": "Не удалось найти данные АЗС на странице"}

    # Получаем или создаём город
    async with AsyncSessionLocal() as db:
        city = await get_or_create_city(db, city_name)
        if not city:
            return {"error": f"Не удалось создать город {city_name}"}

        # Устанавливаем слаг
        await set_city_slug(db, city.id, slug)

        # Получаем список уже существующих станций в городе, чтобы избежать дублей
        from database.crud import get_stations_by_city
        existing_stations = await get_stations_by_city(db, city.id)
        existing_names = {s.name.lower(): s for s in existing_stations}
        existing_addresses = {s.address.lower(): s for s in existing_stations}

        created = 0
        updated_prices = 0

        for match in matches:
            try:
                lat = float(match[0])
                lon = float(match[1])
                raw_name = match[2].strip()
                address = match[3].strip()
                fuel_data = match[4] if len(match) > 4 else ''
                price_str = match[5] if len(match) > 5 else ''

                # Извлекаем цену АИ-95
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
                    # Если цена не найдена, используем 0 или пропускаем
                    continue

                # Определяем бренд из названия
                brand = None
                brand_keywords = ['Лукойл', 'Газпромнефть', 'КрасноярскНП', 'Кит', 'ОПТИ', 'Роснефть', 'ТНК', 'Shell', 'BP', 'Tatneft']
                for b in brand_keywords:
                    if b.lower() in raw_name.lower():
                        brand = b
                        break

                # Проверяем, существует ли уже станция с таким названием и адресом
                station = None
                norm_name = raw_name.lower()
                norm_address = address.lower() if address else ''
                if norm_name in existing_names:
                    station = existing_names[norm_name]
                elif norm_address and norm_address in existing_addresses:
                    station = existing_addresses[norm_address]
                else:
                    # Создаём новую станцию
                    station = await create_station(
                        db,
                        city_id=city.id,
                        name=raw_name,
                        address=address,
                        lat=lat,
                        lon=lon,
                        brand=brand
                    )
                    created += 1
                    existing_names[norm_name] = station
                    if norm_address:
                        existing_addresses[norm_address] = station

                # Сохраняем цену (история)
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
                logger.error(f"Ошибка при обработке записи: {e}")
                continue

        await db.commit()

        return {
            "city": city_name,
            "slug": slug,
            "stations_created": created,
            "prices_updated": updated_prices
        }
