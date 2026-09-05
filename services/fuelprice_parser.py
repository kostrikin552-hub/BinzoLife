# services/fuelprice_parser.py — ПОЛНАЯ ИСПРАВЛЕННАЯ ВЕРСИЯ
import asyncio
import json
import logging
import random
import re
from typing import List, Dict, Optional
import aiohttp
from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from database.models import City, User
from database.crud import update_city_prices_by_brand
from utils.task_locks import task_locker

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.6167.178 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
]

class HybridFuelParser:
    """Двухконтурный гибридный парсер (FuelPrices + 2ГИС) с защитой от изменения верстки"""

    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=30)  # увеличен таймаут

    def _get_headers(self, referer: str = "https://yandex.ru/") -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": referer,
            "Cache-Control": "no-cache",
            "Upgrade-Insecure-Requests": "1"
        }

    def _normalize_fuel_name(self, raw_name: str) -> Optional[str]:
        raw_upper = raw_name.upper().replace(" ", "")
        if "100" in raw_upper:
            return "АИ-100"
        elif "98" in raw_upper:
            return "АИ-98"
        elif "95" in raw_upper:
            return "АИ-95"
        elif "92" in raw_upper:
            return "АИ-92"
        elif "ДТ" in raw_upper or "ДИЗЕЛ" in raw_upper:
            return "ДТ"
        elif "ГАЗ" in raw_upper or "ПРОПАН" in raw_upper or "МЕТАН" in raw_upper:
            return "ГАЗ"
        return None

    def _detect_brand(self, text: str) -> Optional[str]:
        brands = ["Лукойл", "Газпромнефть", "Татнефть", "Роснефть", "Башнефть", "Teboil", "Шелл", "Shell"]
        for b in brands:
            if b.lower() in text.lower():
                return b
        return None

    # ============================================================
    # 1. МНОГОУРОВНЕВЫЙ СБОР С FUELPRICES.RU (С ПОВТОРНЫМИ ПОПЫТКАМИ)
    # ============================================================
    async def fetch_fuelprices_city(self, city_slug: str) -> List[Dict]:
        """Сбор средних цен топлива с сайта fuelprices.ru с несколькими эвристиками и повторными попытками"""
        url = f"https://fuelprices.ru/{city_slug}"
        results = []
        for attempt in range(1, 6):  # увеличено до 6 попыток
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.get(url, headers=self._get_headers("https://fuelprices.ru/")) as response:
                        if response.status == 404:
                            logger.warning(f"FuelPrices вернул 404 для города со слагом '{city_slug}'. Проверьте правильность слага.")
                            return []
                        if response.status in (502, 503, 504, 522):
                            logger.warning(f"FuelPrices {city_slug}: HTTP статус {response.status} (попытка {attempt}), повтор через {attempt*3} сек.")
                            await asyncio.sleep(attempt * 3)
                            continue
                        if response.status != 200:
                            logger.warning(f"FuelPrices {city_slug}: HTTP статус {response.status} (попытка {attempt})")
                            await asyncio.sleep(attempt * 2)
                            continue
                        html = await response.text()
                        soup = BeautifulSoup(html, "html.parser")

                        # --- Эвристика 1: Поиск по таблицам ---
                        tables = soup.find_all("table")
                        for table in tables:
                            rows = table.find_all("tr")
                            for row in rows:
                                cols = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                                if len(cols) >= 2:
                                    fuel_type = self._normalize_fuel_name(cols[0])
                                    if not fuel_type:
                                        continue
                                    for col_text in cols[1:]:
                                        match = re.search(r'(\d{2}[.,]\d{2})', col_text)
                                        if match:
                                            try:
                                                price_val = float(match.group(1).replace(",", "."))
                                                if 30.0 <= price_val <= 150.0:
                                                    results.append({
                                                        "fuel_type": fuel_type,
                                                        "price": price_val,
                                                        "source": "fuelprices.ru",
                                                        "brand": self._detect_brand(cols[0])
                                                    })
                                                    break
                                            except ValueError:
                                                continue

                        # --- Эвристика 2: Если таблицы не сработали, ищем блоки карточек ---
                        if not results:
                            blocks = soup.find_all(["div", "li", "p"], class_=re.compile(r'(price|fuel|item|cost)', re.I))
                            for block in blocks:
                                text = block.get_text(" ", strip=True)
                                fuel_type = self._normalize_fuel_name(text)
                                if fuel_type:
                                    match = re.search(r'(\d{2}[.,]\d{2})', text)
                                    if match:
                                        try:
                                            price_val = float(match.group(1).replace(",", "."))
                                            if 30.0 <= price_val <= 150.0:
                                                results.append({
                                                    "fuel_type": fuel_type,
                                                    "price": price_val,
                                                    "source": "fuelprices.ru",
                                                    "brand": self._detect_brand(text)
                                                })
                                        except ValueError:
                                            continue

                        # --- Эвристика 3: Регулярные выражения по всему тексту страницы ---
                        if not results:
                            patterns = [
                                r'(АИ[- ]?92|АИ[- ]?95|АИ[- ]?98|АИ[- ]?100|ДТ|Дизель)[^0-9\n\r]{1,20}(\d{2}[.,]\d{2})',
                            ]
                            for pattern in patterns:
                                matches = re.findall(pattern, html, re.IGNORECASE)
                                for f_raw, p_raw in matches:
                                    fuel_type = self._normalize_fuel_name(f_raw)
                                    if fuel_type:
                                        try:
                                            price_val = float(p_raw.replace(",", "."))
                                            if 30.0 <= price_val <= 150.0:
                                                results.append({
                                                    "fuel_type": fuel_type,
                                                    "price": price_val,
                                                    "source": "fuelprices.ru",
                                                    "brand": None
                                                })
                                        except ValueError:
                                            continue

                        # Дедупликация (оставляем уникальные fuel_type + brand)
                        if results:
                            unique_results = {}
                            for r in results:
                                key = (r["fuel_type"], r.get("brand"))
                                if key not in unique_results:
                                    unique_results[key] = r
                            final_list = list(unique_results.values())
                            logger.info(f"✅ FuelPrices: успешно извлечено {len(final_list)} цен для '{city_slug}'")
                            return final_list
            except asyncio.TimeoutError:
                logger.warning(f"FuelPrices {city_slug}: таймаут (попытка {attempt}), повтор через {attempt*3} сек.")
                await asyncio.sleep(attempt * 3)
            except Exception as e:
                logger.debug(f"FuelPrices attempt {attempt} для '{city_slug}' завершилась ошибкой: {e}")
                await asyncio.sleep(attempt * 2.0)

        logger.warning(f"⚠️ FuelPrices не вернул цен для '{city_slug}' после 5 попыток.")
        return []

    # ============================================================
    # 2. РЕЗЕРВНЫЙ СБОР ЧЕРЕЗ 2ГИС
    # ============================================================
    async def fetch_2gis_stations_prices(self, city_query: str, fuel_type: str = "АИ-95") -> List[Dict]:
        """Сбор данных с каталога 2ГИС через JSON-LD"""
        encoded_query = aiohttp.helpers.quote(f"АЗС {fuel_type}", safe="")
        url = f"https://2gis.ru/{city_query}/search/{encoded_query}"
        results = []
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, headers=self._get_headers("https://2gis.ru/")) as response:
                    if response.status == 200:
                        html = await response.text()
                        soup = BeautifulSoup(html, "html.parser")
                        scripts = soup.find_all("script", type="application/ld+json")
                        for script in scripts:
                            if not script.string:
                                continue
                            try:
                                data = json.loads(script.string)
                                items = data if isinstance(data, list) else [data]
                                for item in items:
                                    if item.get("@type") in ("GasStation", "AutoRepair", "LocalBusiness"):
                                        brand = item.get("name")
                                        address = item.get("address", {}).get("streetAddress", "")
                                        geo = item.get("geo", {})
                                        lat = geo.get("latitude")
                                        lon = geo.get("longitude")
                                        if lat and lon:
                                            results.append({
                                                "brand": brand,
                                                "address": address,
                                                "lat": float(lat),
                                                "lon": float(lon),
                                                "source": "2gis"
                                            })
                            except (json.JSONDecodeError, TypeError):
                                continue
        except Exception as e:
            logger.debug(f"2ГИС поиск для {city_query} предупреждение: {e}")
        return results

    # ============================================================
    # 3. ЕЖЕДНЕВНЫЙ ФОНОВЫЙ ЦИКЛ ОБХОДА ВСЕХ ГОРОДОВ
    # ============================================================
    async def run_daily_parse_all_cities(self, session_factory):
        """Интеллектуальный обход всех 65+ городов с защитой от банов и задержкой"""
        if not task_locker.acquire("daily_fuel_parser", timeout_seconds=3600):
            logger.warning("Парсер всех городов уже запущен. Пропускаем дублирующий запуск.")
            return
        try:
            logger.info("🚀 Запуск планового обхода всех городов РФ...")
            async with session_factory() as session:
                # Находим города с зарегистрированными пользователями
                active_city_ids = set(
                    (await session.execute(
                        select(User.city_id).where(User.city_id.isnot(None)).distinct()
                    )).scalars().all()
                )
                # Жадная загрузка city_slug предотвращает DetachedInstanceError
                all_cities = (await session.execute(
                    select(City)
                    .options(joinedload(City.city_slug))
                    .where(City.is_active.is_(True))
                )).scalars().all()

                # Сортировка: сначала города с живыми водителями
                sorted_cities = sorted(
                    all_cities,
                    key=lambda c: 0 if c.id in active_city_ids else 1
                )

            total_updated = 0
            cities_processed = 0
            for idx, city in enumerate(sorted_cities, start=1):
                slug = city.slug
                if not slug and city.city_slug:
                    slug = city.city_slug.slug
                if not slug:
                    logger.warning(f"[{idx}/{len(sorted_cities)}] У города '{city.name}' нет слага. Пропускаем.")
                    continue

                cities_processed += 1
                logger.info(f"[{idx}/{len(sorted_cities)}] Парсинг цен для г. {city.name} (slug: {slug})...")
                try:
                    prices = await self.fetch_fuelprices_city(slug)
                    if prices:
                        async with session_factory() as session:
                            updated_count = 0
                            for item in prices:
                                count = await update_city_prices_by_brand(
                                    session=session,
                                    city_id=city.id,
                                    fuel_type=item["fuel_type"],
                                    price=item["price"],
                                    brand_pattern=item.get("brand"),
                                    source=item["source"]
                                )
                                updated_count += count
                            await session.commit()
                            total_updated += updated_count
                            logger.info(f"Обновлено {updated_count} станций в г. {city.name}")
                    else:
                        logger.info(f"FuelPrices пуст для {city.name}, пробуем 2ГИС...")
                        await self.fetch_2gis_stations_prices(slug)
                except Exception as err:
                    logger.error(f"Сбой при обработке города {city.name}: {err}")
                    await asyncio.sleep(2.0)
                    continue

                # Задержка 3.0–4.5 сек между городами (защита от банов)
                await asyncio.sleep(random.uniform(3.0, 4.5))

            logger.info(f"🎉 Ежедневный сбор завершён: обработано городов: {cities_processed}, обновлено котировок на АЗС: {total_updated}")
        finally:
            task_locker.release("daily_fuel_parser")

fuel_parser = HybridFuelParser()

async def fuel_price_parser_worker():
    from database.session import AsyncSessionLocal
    logger.info("[FuelPriceParser] Фоновый воркер запущен.")
    await asyncio.sleep(30)
    while True:
        try:
            await fuel_parser.run_daily_parse_all_cities(AsyncSessionLocal)
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            logger.info("[FuelPriceParser] Воркер остановлен.")
            break
        except Exception as e:
            logger.error(f"[FuelPriceParser] Ошибка цикла: {e}", exc_info=True)
            await asyncio.sleep(300)
