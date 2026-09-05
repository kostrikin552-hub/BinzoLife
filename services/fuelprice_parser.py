# services/fuelprice_parser.py
import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from typing import List, Dict, Optional
import aiohttp
from bs4 import BeautifulSoup
from sqlalchemy import select

from database.models import City, FuelPrice, Station, User
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
    """Двухконтурный гибридный парсер (FuelPrices + 2ГИС)"""

    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=18)

    def _get_headers(self, referer: str = "https://yandex.ru/") -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": referer,
            "Cache-Control": "no-cache",
        }

    # ============================================================
    # 1. СБОР С FUELPRICES.RU
    # ============================================================
    async def fetch_fuelprices_city(self, city_slug: str) -> List[Dict]:
        """Сбор средних цен топлива с портала fuelprices.ru"""
        url = f"https://fuelprices.ru/{city_slug}"
        results = []
        for attempt in range(1, 4):
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.get(url, headers=self._get_headers("https://fuelprices.ru/")) as response:
                        if response.status == 200:
                            html = await response.text()
                            soup = BeautifulSoup(html, "html.parser")
                            tables = soup.find_all("table")
                            for table in tables:
                                rows = table.find_all("tr")
                                for row in rows[1:]:
                                    cols = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                                    if len(cols) >= 2:
                                        fuel_raw = cols[0].upper()
                                        price_str = cols[1].replace(",", ".").replace("₽", "").strip()
                                        
                                        # Определяем тип топлива
                                        fuel_type = None
                                        brand = None
                                        if "92" in fuel_raw:
                                            fuel_type = "АИ-92"
                                        elif "95" in fuel_raw:
                                            fuel_type = "АИ-95"
                                        elif "98" in fuel_raw:
                                            fuel_type = "АИ-98"
                                        elif "100" in fuel_raw:
                                            fuel_type = "АИ-100"
                                        elif "ДТ" in fuel_raw or "ДИЗЕЛ" in fuel_raw:
                                            fuel_type = "ДТ"
                                        elif "ГАЗ" in fuel_raw or "ПРОПАН" in fuel_raw or "МЕТАН" in fuel_raw:
                                            fuel_type = "ГАЗ"
                                        if not fuel_type:
                                            continue
                                        
                                        # Попробуем извлечь бренд (если есть в названии колонки)
                                        if "Лукойл" in fuel_raw:
                                            brand = "Лукойл"
                                        elif "Газпромнефть" in fuel_raw or "ГПН" in fuel_raw:
                                            brand = "Газпромнефть"
                                        elif "Татнефть" in fuel_raw:
                                            brand = "Татнефть"
                                        elif "Роснефть" in fuel_raw:
                                            brand = "Роснефть"
                                        # и т.д.
                                        
                                        try:
                                            price_val = float(price_str)
                                            if 30.0 <= price_val <= 150.0:
                                                results.append({
                                                    "fuel_type": fuel_type,
                                                    "price": price_val,
                                                    "source": "fuelprices.ru",
                                                    "brand": brand  # может быть None
                                                })
                                        except ValueError:
                                            continue
                            if results:
                                logger.info(f"FuelPrices: получено {len(results)} котировок по {city_slug}")
                                return results
                        elif response.status in (429, 502, 503):
                            await asyncio.sleep(attempt * 2.5)
                            continue
            except Exception as e:
                logger.debug(f"FuelPrices attempt {attempt} для {city_slug} ошибка: {e}")
                await asyncio.sleep(attempt * 2.0)
        return results

    # ============================================================
    # 2. СБОР С 2ГИС (Резервный и уточняющий контур)
    # ============================================================
    async def fetch_2gis_stations_prices(self, city_query: str, fuel_type: str = "АИ-95") -> List[Dict]:
        """Сбор данных о наличии и ценах с каталога 2ГИС через JSON-LD"""
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
            logger.debug(f"2ГИС поиск для {city_query} завершился с предупреждением: {e}")
        return results

    # ============================================================
    # 3. ЕЖЕДНЕВНЫЙ ФОНОВЫЙ ЦИКЛ ПО ВСЕМ ГОРОДАМ
    # ============================================================
    async def run_daily_parse_all_cities(self, session_factory):
        """Интеллектуальный обход 65+ городов с приоритизацией и задержкой"""
        if not task_locker.acquire("daily_fuel_parser", timeout_seconds=3600):
            logger.warning("Парсер всех городов уже выполняется, повторный вызов отклонён.")
            return
        try:
            logger.info("🚀 Запуск планового парсинга цен по городам РФ...")
            async with session_factory() as session:
                active_city_ids = set(
                    (await session.execute(
                        select(User.city_id).where(User.city_id.isnot(None)).distinct()
                    )).scalars().all()
                )
                all_cities = (await session.execute(
                    select(City).where(City.is_active.is_(True))
                )).scalars().all()

                sorted_cities = sorted(
                    all_cities,
                    key=lambda c: 0 if c.id in active_city_ids else 1
                )

            total_updated = 0
            for idx, city in enumerate(sorted_cities, start=1):
                try:
                    logger.info(f"[{idx}/{len(sorted_cities)}] Обновление цен для г. {city.name} (slug: {city.slug})...")
                    prices = await self.fetch_fuelprices_city(city.slug)
                    if prices:
                        async with session_factory() as session:
                            for item in prices:
                                await update_city_prices_by_brand(
                                    session=session,
                                    city_id=city.id,
                                    fuel_type=item["fuel_type"],
                                    price=item["price"],
                                    brand_pattern=item.get("brand"),
                                    source=item["source"]
                                )
                            await session.commit()
                            total_updated += len(prices)
                    else:
                        logger.info(f"FuelPrices пуст для {city.name}, пробуем 2ГИС...")
                        # Здесь можно обработать данные 2ГИС, например, создать/обновить станции
                        # или просто пропустить. Пока оставляем как заглушку.
                        await self.fetch_2gis_stations_prices(city.slug)

                    await asyncio.sleep(random.uniform(3.0, 4.5))
                except Exception as err:
                    logger.error(f"Сбой при обработке города {city.name}: {err}")
                    await asyncio.sleep(2.0)
                    continue

            logger.info(f"✅ Ежедневный сбор завершён. Обновлено {total_updated} цен по {len(sorted_cities)} городам.")
        finally:
            task_locker.release("daily_fuel_parser")


# Глобальный экземпляр парсера
fuel_parser = HybridFuelParser()
