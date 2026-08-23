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
    get_city_slug, set_city_slug, get_station_by_name_address
)
from database.models import FuelType, SourceType

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

                soup = BeautifulSoup(html, 'lxml')
                # Ищем все блоки АЗС – обычно это div с классом "station" или "item"
                # На сайте fuelprice.ru структура: каждый блок АЗС содержит название, адрес, цены
                # Используем поиск по тегам: ищем заголовки с названием, затем ищем цену АИ-95
                
                # Найдём все элементы, содержащие адрес и цены
                # Альтернатива: найти все span с классом "price" и пройти по родительским блокам
                station_blocks = soup.find_all('div', class_=re.compile(r'station|item|card'))
                if not station_blocks:
                    # попробуем другой подход: ищем все блоки, содержащие "Аи-95" или "АИ-95"
                    blocks = soup.find_all(['div', 'section'], recursive=True)
                    station_blocks = []
                    for block in blocks:
                        if block.find(string=re.compile(r'А[иИ]-95')):
                            station_blocks.append(block)
                if not station_blocks:
                    logger.error(f"Не найдены блоки АЗС на странице для {city_name}")
                    continue

                # Получаем список всех станций в БД для сопоставления
                city = await get_city_by_name(db, city_name)
                if not city:
                    logger.warning(f"Город {city_name} не найден в БД")
                    return
                stations = await get_all_active_stations_by_city(db, city.id)
                if not stations:
                    logger.warning(f"В городе {city_name} нет АЗС в БД")
                    return

                # Сопоставляем каждую станцию с ценой
                updated_count = 0
                for block in station_blocks:
                    # Извлекаем название и адрес из блока
                    # Название обычно в теге <h3> или <strong>
                    name_tag = block.find(['h3', 'strong', 'span'], class_=re.compile(r'name|title'))
                    if not name_tag:
                        name_tag = block.find(['h3', 'strong'])
                    name = name_tag.get_text(strip=True) if name_tag else None
                    
                    # Адрес – часто в теге с классом address
                    addr_tag = block.find(['span', 'div'], class_=re.compile(r'address|addr'))
                    if not addr_tag:
                        addr_tag = block.find('span', string=re.compile(r'ул|просп|пер|шоссе|бульвар|пл'))
                    address = addr_tag.get_text(strip=True) if addr_tag else None

                    # Ищем цену АИ-95
                    price = None
                    # Ищем текст "Аи-95" или "АИ-95" и рядом цену
                    price_pattern = re.compile(r'А[иИ]-95\s*[:：]\s*([\d.,]+)')
                    block_text = block.get_text()
                    match = price_pattern.search(block_text)
                    if match:
                        price_text = match.group(1).replace(',', '.').replace(' ', '')
                        try:
                            price = float(price_text)
                        except ValueError:
                            pass
                    else:
                        # Ищем span с классом price после упоминания АИ-95
                        # Попробуем найти все элементы с ценой и выбрать тот, который рядом с АИ-95
                        price_spans = block.find_all('span', class_=re.compile(r'price|cost'))
                        for span in price_spans:
                            if 'АИ-95' in span.previous_sibling or 'Аи-95' in span.previous_sibling:
                                try:
                                    price = float(span.get_text(strip=True).replace(',', '.'))
                                    break
                                except:
                                    pass
                    if not price:
                        continue

                    # Ищем станцию в БД по названию и адресу
                    station = None
                    if name and address:
                        station = await get_station_by_name_address(db, city.id, name, address)
                    if not station and name:
                        # попробуем по названию (частичному совпадению)
                        for s in stations:
                            if name.lower() in s.name.lower() or s.name.lower() in name.lower():
                                station = s
                                break
                    if not station and address:
                        for s in stations:
                            if address.lower() in s.address.lower() or s.address.lower() in address.lower():
                                station = s
                                break
                    if not station:
                        logger.warning(f"Не найдена станция для {name} / {address}, пропускаем")
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
