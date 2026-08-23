import aiohttp
import asyncio
import logging
import traceback
import re
from datetime import datetime, timezone
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
}

def normalize_name(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r'\b(ИНН\s*\d+|ОАО|АО|ЗАО|ООО|ООО\s*"|"|\(|\)|№|\d+)\s*', '', name, flags=re.I)
    name = re.sub(r'\s+', ' ', name).strip()
    return name.lower()

def get_brand_from_name(name: str) -> str:
    name_lower = name.lower()
    if 'лукойл' in name_lower:
        return 'Лукойл'
    elif 'газпромнефть' in name_lower:
        return 'Газпромнефть'
    elif 'красноярскнп' in name_lower or 'красноярскнефтепродукт' in name_lower:
        return 'КрасноярскНП'
    elif 'кит' in name_lower:
        return 'Кит'
    elif 'опти' in name_lower:
        return 'ОПТИ'
    elif 'роснефть' in name_lower:
        return 'Роснефть'
    elif 'тнк' in name_lower:
        return 'ТНК'
    return None

async def fetch_fuelprice_prices(city_name: str = "Красноярск", retries: int = 3):
    logger.info(f"=== fetch_fuelprice_prices() для {city_name} ===")
    async with AsyncSessionLocal() as db:
        slug = await get_city_slug(db, city_name)
        if not slug:
            slug = FALLBACK_SLUGS.get(city_name)
            if not slug:
                logger.error(f"Нет слага для города {city_name}")
                return
            city = await get_city_by_name(db, city_name)
            if city:
                await set_city_slug(db, city.id, slug)
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

                # Ищем блоки с ценами в JSON-подобной структуре
                # Пример: [56.0109, 92.8525, 'Название', 'Адрес', 'Данные', 'Цена', ...]
                pattern = re.compile(r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*([^,]+),\s*([^,]+),\s*([^\]]+)\]')
                matches = pattern.findall(html)
                if not matches:
                    logger.error(f"Не найдены данные станций для {city_name}")
                    continue

                # Получаем все станции из БД для этого города
                city = await get_city_by_name(db, city_name)
                if not city:
                    logger.warning(f"Город {city_name} не найден в БД")
                    return
                stations = await get_all_active_stations_by_city(db, city.id)
                if not stations:
                    logger.warning(f"В городе {city_name} нет АЗС в БД, обновление невозможно")
                    return

                updated_count = 0
                for match in matches:
                    try:
                        lat = float(match[0])
                        lon = float(match[1])
                        raw_name = match[2].strip()
                        # match[3] может быть адресом, но часто содержит HTML-код и цены — проверим
                        raw_address = match[3].strip()
                        # Проверяем, что адрес не содержит HTML-тегов и не слишком длинный
                        if '<' in raw_address or '>' in raw_address or len(raw_address) > 200:
                            raw_address = None  # невалидный адрес
                        # Данные о ценах в match[4] или match[5]
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
                            continue

                        # Сопоставляем с существующей станцией
                        station = None

                        # 1. По координатам (радиус 2 км)
                        for s in stations:
                            dist = haversine_distance(lat, lon, s.latitude, s.longitude)
                            if dist < 2.0:  # 2 км
                                station = s
                                break

                        # 2. По названию (нормализованному)
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
                            # Если не найдена, пропускаем (не создаём новую)
                            logger.warning(f"Не найдена станция для '{raw_name}' (коорд. {lat},{lon}) — пропускаем")
                            continue

                        # Обновляем цену
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
                        await db.rollback()  # откат для этой итерации
                        continue

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
