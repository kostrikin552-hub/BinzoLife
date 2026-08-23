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

                # ---- Парсинг через регулярные выражения ----
                # Ищем все блоки АЗС: они начинаются с заголовка (название) и содержат адрес и цены
                # Паттерн: ищем строки с "Аи-95" или "АИ-95", затем извлекаем цену, а выше ищем название и адрес

                # Разбиваем HTML на строки
                lines = html.split('\n')
                station_blocks = []
                current_block = []
                in_block = False

                # Проходим по строкам, собирая блоки между заголовками (крупный текст)
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    # Признак начала нового блока: строка содержит название сети (например, "ЛУКОЙЛ", "Газпромнефть", "КрасноярскНП") 
                    # и не содержит "Аи-95" (это внутри блока)
                    if re.search(r'(ЛУКОЙЛ|Газпромнефть|КрасноярскНП|Красноярскнефтепродукт|Кит|ОПТИ|ТНК|Роснефть|Shell|BP|Tatneft|Башнефть|Сургутнефтегаз)', line, re.I):
                        if current_block:
                            station_blocks.append('\n'.join(current_block))
                        current_block = [line]
                        in_block = True
                    elif in_block:
                        current_block.append(line)
                        # Если строка содержит цену АИ-95, это конец блока? нет, продолжаем до следующего заголовка
                if current_block:
                    station_blocks.append('\n'.join(current_block))

                logger.info(f"Найдено {len(station_blocks)} блоков АЗС")

                # Получаем список станций в БД
                city = await get_city_by_name(db, city_name)
                if not city:
                    logger.warning(f"Город {city_name} не найден в БД")
                    return
                stations = await get_all_active_stations_by_city(db, city.id)
                if not stations:
                    logger.warning(f"В городе {city_name} нет АЗС в БД")
                    return

                updated_count = 0
                for block in station_blocks:
                    # Извлекаем название (первая строка)
                    block_lines = block.split('\n')
                    name = block_lines[0].strip() if block_lines else None
                    if not name:
                        continue

                    # Извлекаем адрес (строка, содержащая "ул", "пер", "шоссе" и т.д.)
                    address = None
                    for line in block_lines:
                        if re.search(r'(ул|пер|шоссе|бульвар|пл|просп|пр-кт|пр-т|пр.)', line, re.I):
                            address = line.strip()
                            break
                    if not address:
                        # попробуем взять вторую строку, если она не содержит цен
                        if len(block_lines) > 1 and not re.search(r'А[иИ]-95', block_lines[1]):
                            address = block_lines[1].strip()

                    # Извлекаем цену АИ-95
                    price = None
                    for line in block_lines:
                        # Ищем паттерн "Аи-95 : 84.9" или "АИ-95 : 84.9"
                        match = re.search(r'А[иИ]-95\s*[:：]\s*([\d.,]+)', line)
                        if match:
                            price_text = match.group(1).replace(',', '.').replace(' ', '')
                            try:
                                price = float(price_text)
                            except ValueError:
                                pass
                            break
                    if not price:
                        # дополнительный поиск: может быть написано "АИ-95 84.9"
                        for line in block_lines:
                            match = re.search(r'А[иИ]-95\s+([\d.,]+)', line)
                            if match:
                                price_text = match.group(1).replace(',', '.').replace(' ', '')
                                try:
                                    price = float(price_text)
                                except ValueError:
                                    pass
                                break

                    if not price:
                        logger.warning(f"Не найдена цена для блока: {name[:30]}")
                        continue

                    # Сопоставляем с БД
                    station = None
                    # 1. по имени + адресу
                    if name and address:
                        station = await get_station_by_name_address(db, city.id, name, address)
                    # 2. по частичному совпадению имени
                    if not station:
                        for s in stations:
                            if name.lower() in s.name.lower() or s.name.lower() in name.lower():
                                station = s
                                break
                    # 3. по частичному совпадению адреса
                    if not station and address:
                        for s in stations:
                            if address.lower() in s.address.lower() or s.address.lower() in address.lower():
                                station = s
                                break
                    if not station:
                        # Если станция не найдена, создаём новую? Лучше пропустить, чтобы не плодить дубли.
                        logger.warning(f"Не найдена станция для '{name}' / '{address}', пропускаем")
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
