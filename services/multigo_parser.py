# services/multigo_parser.py — ОБНОВЛЁННАЯ ВЕРСИЯ С САНИТАЙЗЕРОМ
import asyncio
import logging
import random
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timezone
import aiohttp

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from database.models import City, User, Station, FuelPrice, FuelType, SourceType
from database.crud import commit_or_rollback
from utils.task_locks import task_locker
from services.data_sanitizer import clean_brand_name, validate_fuel_price, normalize_address

logger = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

FUEL_TYPE_MAP = {
    "a92": FuelType.AI_92,
    "a95": FuelType.AI_95,
    "a98": FuelType.AI_98,
    "a100": FuelType.AI_100,
    "dt": FuelType.DT,
    "diesel": FuelType.DT,
}


class MultiGoParser:
    """Асинхронный сборщик АЗС и цен через JSON API MultiGo с санитайзером"""

    def __init__(self):
        self.base_url = "https://multigo.ru/api"
        self.timeout = aiohttp.ClientTimeout(total=20, connect=8)
        self.session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
                    "Origin": "https://multigo.ru",
                    "Referer": "https://multigo.ru/",
                }
            )
        return self.session

    def _headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Referer": "https://multigo.ru/",
            "Origin": "https://multigo.ru",
        }

    def _get_bounding_box(self, lat: float, lon: float, delta: float = 0.3) -> Dict[str, float]:
        return {
            "south": lat - delta,
            "north": lat + delta,
            "west": lon - delta * 1.5,
            "east": lon + delta * 1.5,
        }

    async def fetch_stations_in_bbox(self, lat: float, lon: float) -> List[Dict]:
        """Запрашивает все АЗС в радиусе города по координатам центра."""
        bbox = self._get_bounding_box(lat, lon)
        url = (
            f"{self.base_url}/geo/stations"
            f"?box={bbox['south']},{bbox['west']},{bbox['north']},{bbox['east']}"
        )

        for attempt in range(1, 4):
            try:
                session = await self._get_session()
                async with session.get(url, headers=self._headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data if isinstance(data, list) else data.get("stations", data.get("items", []))
                        stations_result = []

                        for item in items:
                            st_id = item.get("id")
                            raw_brand = item.get("brand_name") or item.get("brand") or item.get("title") or "АЗС"
                            raw_address = item.get("address") or item.get("location") or ""
                            st_lat = item.get("lat") or item.get("latitude")
                            st_lon = item.get("lon") or item.get("longitude")

                            # 1. Санитайзинг бренда
                            brand = clean_brand_name(raw_brand)

                            # 2. Санитайзинг адреса
                            address = normalize_address(raw_address)

                            raw_prices = item.get("prices", {})
                            fuel_prices = []
                            if isinstance(raw_prices, dict):
                                for key, fuel_type in FUEL_TYPE_MAP.items():
                                    if key in raw_prices:
                                        try:
                                            raw_price = float(raw_prices[key])
                                            # 3. Валидация цены
                                            validated_price = validate_fuel_price(fuel_type.value, raw_price)
                                            if validated_price is not None:
                                                fuel_prices.append({
                                                    "fuel_type": fuel_type,
                                                    "price": validated_price
                                                })
                                        except (ValueError, TypeError):
                                            continue

                            if st_lat and st_lon and fuel_prices:
                                stations_result.append({
                                    "external_id": st_id,
                                    "brand": brand,
                                    "address": address,
                                    "lat": float(st_lat),
                                    "lon": float(st_lon),
                                    "prices": fuel_prices
                                })

                        logger.info(f"[MultiGo] Получено {len(stations_result)} АЗС с ценами вокруг ({lat}, {lon})")
                        return stations_result

                    elif resp.status == 429:
                        logger.warning(f"[MultiGo] 429 Too Many Requests, пауза {attempt * 3} сек...")
                        await asyncio.sleep(attempt * 3)
                    else:
                        logger.warning(f"[MultiGo] HTTP статус {resp.status} на попытке {attempt}")
                        await asyncio.sleep(attempt * 2)

            except asyncio.TimeoutError:
                logger.warning(f"[MultiGo] Таймаут (попытка {attempt})")
                await asyncio.sleep(attempt * 3)
            except Exception as e:
                logger.debug(f"[MultiGo] Ошибка попытки {attempt}: {e}")
                await asyncio.sleep(attempt * 2)

        return []

    async def sync_stations_batch(self, session, city_id: int, stations_data: List[Dict]) -> Tuple[int, int]:
        """
        Создаёт или находит станции по координатам и обновляет их цены.
        Все данные проходят через санитайзер.
        """
        now = datetime.now(timezone.utc)
        stations_synced = 0
        prices_synced = 0

        if not stations_data:
            return 0, 0

        # Загружаем существующие станции города
        stmt = select(Station).where(Station.city_id == city_id, Station.is_active == True)
        existing_stations = (await session.execute(stmt)).scalars().all()

        # Индекс существующих станций по координатам (округление до ~100 метров)
        coord_index = {
            (round(s.latitude, 3), round(s.longitude, 3)): s
            for s in existing_stations if s.latitude and s.longitude
        }

        for item in stations_data:
            lat = item["lat"]
            lon = item["lon"]
            key = (round(lat, 3), round(lon, 3))

            # Бренд уже очищен в fetch_stations_in_bbox, но на всякий случай
            brand = clean_brand_name(item["brand"]) if item["brand"] else "АЗС"
            address = normalize_address(item["address"])

            station = coord_index.get(key)
            if not station:
                # Создаём новую станцию
                station = Station(
                    city_id=city_id,
                    name=brand[:200] or "АЗС",
                    brand=brand,
                    address=address[:300] if address else "Адрес уточняется",
                    latitude=lat,
                    longitude=lon,
                    is_active=True
                )
                session.add(station)
                await session.flush()
                coord_index[key] = station
                stations_synced += 1
            else:
                # Если у существующей станции не было адреса, дополняем его
                if (not station.address or station.address == "" or "уточн" in station.address.lower()) and address:
                    station.address = address[:300]
                    await session.flush()
                # Если бренд изменился (например, «Лукойл» вместо «ООО Лукойл»), обновляем
                if station.brand != brand:
                    station.brand = brand
                    station.name = brand[:200]
                    await session.flush()

            # Сохраняем цены (уже валидированные)
            for p in item["prices"]:
                fuel_type = p["fuel_type"]
                price_val = p["price"]

                # Находим существующую свежую цену
                p_stmt = select(FuelPrice).where(
                    FuelPrice.station_id == station.id,
                    FuelPrice.fuel_type == fuel_type,
                    FuelPrice.is_fresh == True
                )
                existing_price = (await session.execute(p_stmt)).scalar_one_or_none()

                if existing_price:
                    existing_price.price = price_val
                    existing_price.source = SourceType.PARSER
                    existing_price.recorded_at = now
                else:
                    new_price = FuelPrice(
                        station_id=station.id,
                        fuel_type=fuel_type,
                        price=price_val,
                        source=SourceType.PARSER,
                        confidence=0.85,
                        is_fresh=True,
                        recorded_at=now
                    )
                    session.add(new_price)
                prices_synced += 1

        await commit_or_rollback(session)
        return stations_synced, prices_synced

    async def run_daily_parse_all_cities(self, session_factory):
        """Ежедневный обход всех городов через MultiGo"""
        if not task_locker.acquire("daily_multigo_parser", timeout_seconds=7200):
            logger.warning("Парсер MultiGo уже выполняется, пропуск.")
            return

        try:
            logger.info("🚀 Запуск обновления цен через MultiGo API...")
            await asyncio.sleep(random.uniform(3, 8))

            async with session_factory() as session:
                active_city_ids = set(
                    (await session.execute(
                        select(User.city_id).where(User.city_id.isnot(None)).distinct()
                    )).scalars().all()
                )
                all_cities = (await session.execute(
                    select(City).where(City.is_active.is_(True))
                )).scalars().all()

                sorted_cities = sorted(all_cities, key=lambda c: 0 if c.id in active_city_ids else 1)

            total_stations = 0
            total_prices = 0

            for idx, city in enumerate(sorted_cities, start=1):
                if city.latitude is None or city.longitude is None:
                    logger.warning(f"[{idx}/{len(sorted_cities)}] Город {city.name} без координат, пропускаем")
                    continue

                logger.info(f"[{idx}/{len(sorted_cities)}] Сбор для г. {city.name} (lat={city.latitude}, lon={city.longitude})...")
                stations_data = await self.fetch_stations_in_bbox(city.latitude, city.longitude)

                if stations_data:
                    async with session_factory() as session:
                        stations_updated, prices_updated = await self.sync_stations_batch(
                            session, city.id, stations_data
                        )
                        total_stations += stations_updated
                        total_prices += prices_updated
                        logger.info(f"Сохранено: {stations_updated} новых станций, {prices_updated} цен для {city.name}")
                else:
                    logger.warning(f"Не удалось получить данные для {city.name}")

                # Пауза 3-5 секунд между городами
                await asyncio.sleep(random.uniform(3.0, 5.0))

            logger.info(f"🎉 Сбор через MultiGo завершён! Всего станций: {total_stations}, цен: {total_prices}")

        finally:
            task_locker.release("daily_multigo_parser")
            if self.session and not self.session.closed:
                await self.session.close()
                self.session = None


multigo_parser = MultiGoParser()


async def multigo_parser_worker():
    """Фоновый воркер для запуска в main.py"""
    from database.session import AsyncSessionLocal
    logger.info("[MultiGoParser] Фоновый воркер запущен.")
    await asyncio.sleep(30)
    while True:
        try:
            await multigo_parser.run_daily_parse_all_cities(AsyncSessionLocal)
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[MultiGoParser] Ошибка: {e}", exc_info=True)
            await asyncio.sleep(300)
