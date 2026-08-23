import aiohttp
import asyncio
import logging
import traceback
import re
from datetime import datetime, timezone
from database.session import AsyncSessionLocal
from database.crud import (
    get_city_by_name, get_all_active_stations_by_city, save_price,
    get_city_slug, set_city_slug, get_station_by_name_address
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

                # ---- Ищем все блоки с ценами, используя JSON-подобную структуру ----
                # На странице fuelprice.ru данные часто встроены в JavaScript объекты
                # Попробуем найти все блоки, содержащие координаты и цены
                # Паттерн: координаты в формате [lat, lon, ...]
                pattern = re.compile(r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*\'([^\']*)\',\s*([^,]+),\s*([^,]+),\s*([^\]]+)\]')
                matches = pattern.findall(html)
                if not matches:
                    # Альтернативный поиск через обычный HTML
                    station_blocks = []
                    lines = html.split('\n')
                    for line in lines:
                        if 'Аи-95' in line or 'АИ-95' in line:
                            # Извлекаем цену
                            match_price = re.search(r'А[иИ]-95\s*[:：]\s*([\d.,]+)', line)
                            if not match_price:
                                match_price = re.search(r'А[иИ]-95\s+([\d.,]+)', line)
                            if match_price:
                                price = float(match_price.group(1).replace(',', '.'))
                                # Ищем название станции выше
                                # Это сложно, поэтому пропускаем, если не нашли в JSON
                                continue
                    if not matches:
                        logger.error(f"Не найдены данные станций для {city_name}")
                        continue

                # Получаем все станции из БД
                city = await get_city_by_name(db, city_name)
                if not city:
                    logger.warning(f"Город {city_name} не найден в БД")
                    return
                stations = await get_all_active_stations_by_city(db, city.id)
                if not stations:
                    logger.warning(f"В городе {city_name} нет АЗС в БД")
                    return

                updated_count = 0
                for match in matches:
                    # match: [lat, lon, name, address, fuel_data, price_str, some_flag, ...]
                    lat = float(match[0])
                    lon = float(match[1])
                    name = match[2].strip()
                    address = match[3].strip()
                    # Цена может быть в match[5] или извлечена отдельно
                    price = None
                    # Ищем в match[4] (fuel_data) или match[5] (price_str)
                    fuel_data = match[4] if len(match) > 4 else ''
                    price_str = match[5] if len(match) > 5 else ''
                    # Пытаемся найти цену в fuel_data
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
                        # Пытаемся найти цену в полном тексте блока (всё, что после match)
                        # Но это сложно, пропускаем
                        continue

                    # Сопоставление с БД по координатам (если расстояние < 0.5 км)
                    station = None
                    for s in stations:
                        dist = haversine_distance(lat, lon, s.latitude, s.longitude)
                        if dist < 0.5:  # 500 метров
                            station = s
                            break
                    # Если не нашли по координатам, пробуем по названию
                    if not station:
                        for s in stations:
                            # Убираем номера, скобки, приводим к нижнему регистру
                            clean_name = re.sub(r'[^\w\s]', '', name.lower())
                            clean_s_name = re.sub(r'[^\w\s]', '', s.name.lower())
                            if clean_name in clean_s_name or clean_s_name in clean_name:
                                station = s
                                break
                    # Если не нашли по названию, пробуем по адресу
                    if not station:
                        for s in stations:
                            if address and s.address and (address.lower() in s.address.lower() or s.address.lower() in address.lower()):
                                station = s
                                break

                    if not station:
                        logger.warning(f"Не найдена станция для '{name}' / '{address}' (коорд. {lat},{lon})")
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
                    logger.info(f"Обновлена цена для {station.name}: {price} ₽ (по координатам)")

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
