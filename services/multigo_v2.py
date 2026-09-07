# services/multigo_v2.py — финальная версия с передачей fuelId и маппингом топлива
import aiohttp
import asyncio
import logging
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timezone

from sqlalchemy import select

from database.models import City, Station, FuelPrice, FuelType, SourceType
from database.crud import commit_or_rollback

logger = logging.getLogger(__name__)

# URL MultiGo API
NEAR_LIST_URL = "https://multigo.ru/api/9/near/list"
AVG_PRICES_URL = "https://multigo.ru/api/9/avgprices"  # пока не используется

# Маппинг fuelId → код топлива в БД (ключи совпадают с FuelType)
MULTIGO_FUEL_MAP = {
    11: "AI-95",
    8: "AI-92",
    3: "DT",
    1: "СУГ",     # или "LPG" в зависимости от вашей модели
    16: "AI-100",
    14: "AI-98",
    4: "DT+",     # если есть
    12: "AI-95+", # если есть
}

# Список топлива для опроса (обязательные fuelId)
FUEL_TYPES_TO_FETCH = [
    {"id": 11, "code": "AI-95"},
    {"id": 8, "code": "AI-92"},
    {"id": 3, "code": "DT"},
    {"id": 1, "code": "СУГ"},
    {"id": 16, "code": "AI-100"},
    {"id": 14, "code": "AI-98"},
]

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
}


class MultiGoV2Service:
    """Сервис синхронизации станций и цен через MultiGo с обязательным fuelId"""

    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=15, connect=8)
        self.session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers=HEADERS
            )
        return self.session

    async def fetch_stations_for_fuel(self, lat: float, lon: float, fuel_id: int, limit: int = 400) -> List[Dict]:
        """
        Запрашивает станции с ценами для конкретного fuelId.
        Без fuelId MultiGo возвращает пустой fuels: []
        """
        payload = {
            "lat": lat,
            "lng": lon,
            "limit": limit,
            "fuelId": fuel_id,  # <-- КЛЮЧЕВОЙ ПАРАМЕТР!
        }
        try:
            session = await self._get_session()
            async with session.post(NEAR_LIST_URL, json=payload) as resp:
                if resp.status != 200:
                    logger.warning(f"[MultiGo] HTTP {resp.status} для fuelId={fuel_id}")
                    return []
                data = await resp.json()
                return data.get("data", {}).get("list", []) or []
        except Exception as e:
            logger.error(f"[MultiGo] Ошибка запроса для fuelId={fuel_id}: {e}")
            return []

    async def collect_city_stations(self, lat: float, lon: float) -> Dict[str, Dict]:
        """
        Собирает станции и цены для всех топлив в радиусе.
        Возвращает словарь {station_id: {name, brand, address, lat, lon, fuels: {AI-95: 58.4, ...}}}
        """
        result: Dict[str, Dict] = {}
        session = await self._get_session()

        for fuel_info in FUEL_TYPES_TO_FETCH:
            fuel_id = fuel_info["id"]
            fuel_code = fuel_info["code"]

            raw_stations = await self.fetch_stations_for_fuel(lat, lon, fuel_id)

            for item in raw_stations:
                st_id = str(item.get("id"))
                if not st_id:
                    continue

                # Если станция ещё не добавлена — создаём запись
                if st_id not in result:
                    brand_info = item.get("brand") or {}
                    brand = brand_info.get("name") if isinstance(brand_info, dict) else None
                    if not brand:
                        brand = item.get("name", "АЗС").split()[0] if item.get("name") else "АЗС"
                    loc = item.get("loc") or [lat, lon]
                    address = item.get("address") or f"Координаты: {loc[0]:.4f}, {loc[1]:.4f}"
                    result[st_id] = {
                        "id": st_id,
                        "name": item.get("name", "АЗС"),
                        "brand": brand,
                        "address": address,
                        "lat": loc[0] if len(loc) > 0 else lat,
                        "lon": loc[1] if len(loc) > 1 else lon,
                        "fuels": {},
                    }

                # Извлекаем цены из поля fuels (обратите внимание на fuelPrice)
                for f in item.get("fuels") or []:
                    f_id = f.get("fuelId")
                    f_price = f.get("fuelPrice")  # правильное поле!
                    if f_id is not None and f_price is not None:
                        try:
                            price_val = float(f_price)
                            mapped_type = MULTIGO_FUEL_MAP.get(int(f_id))
                            if mapped_type and price_val > 0:
                                result[st_id]["fuels"][mapped_type] = price_val
                        except (ValueError, TypeError):
                            continue

            await asyncio.sleep(0.06)  # защита от троттлинга

        return result

    async def sync_city(self, session, city: City) -> Tuple[int, int]:
        """
        Синхронизирует город: получает станции с ценами через MultiGo и сохраняет в БД.
        Возвращает (количество станций, количество цен).
        """
        lat = city.latitude
        lon = city.longitude
        if not lat or not lon:
            logger.warning(f"[MultiGo] Город {city.name} без координат, пропускаем")
            return 0, 0

        logger.info(f"[MultiGo] Сбор данных для {city.name}...")
        stations_dict = await self.collect_city_stations(lat, lon)
        if not stations_dict:
            logger.warning(f"[MultiGo] Не найдено станций для {city.name}")
            return 0, 0

        # Подсчёт цен для лога
        total_prices = sum(len(st["fuels"]) for st in stations_dict.values())
        logger.info(f"[MultiGo] Получено {len(stations_dict)} станций, {total_prices} цен для {city.name}")

        # Сохраняем в БД
        stations_saved = 0
        prices_saved = 0

        # Индекс существующих станций по координатам
        stmt = select(Station).where(Station.city_id == city.id, Station.is_active == True)
        existing = (await session.execute(stmt)).scalars().all()
        coord_index = {
            (round(s.latitude, 3), round(s.longitude, 3)): s
            for s in existing if s.latitude and s.longitude
        }

        now = datetime.now(timezone.utc)

        for st_data in stations_dict.values():
            lat_s = st_data["lat"]
            lon_s = st_data["lon"]
            key = (round(lat_s, 3), round(lon_s, 3))

            station = coord_index.get(key)
            if not station:
                from services.data_sanitizer import clean_brand_name
                brand = clean_brand_name(st_data["brand"])
                station = Station(
                    city_id=city.id,
                    name=st_data["name"][:200],
                    brand=brand,
                    address=st_data["address"][:300],
                    latitude=lat_s,
                    longitude=lon_s,
                    is_active=True,
                )
                session.add(station)
                await session.flush()
                coord_index[key] = station
                stations_saved += 1
            else:
                # Обновляем адрес, если он пустой
                if (not station.address or station.address == "" or "уточн" in station.address.lower()) and st_data["address"]:
                    station.address = st_data["address"][:300]
                    await session.flush()

            # Сохраняем цены для этой станции
            for fuel_name, price_val in st_data["fuels"].items():
                # Преобразуем название топлива в FuelType enum
                fuel_type_enum = None
                for ft in FuelType:
                    if ft.value == fuel_name:
                        fuel_type_enum = ft
                        break
                if not fuel_type_enum:
                    continue

                if price_val < 30 or price_val > 150:
                    continue  # защита от выбросов

                p_stmt = select(FuelPrice).where(
                    FuelPrice.station_id == station.id,
                    FuelPrice.fuel_type == fuel_type_enum,
                    FuelPrice.is_fresh == True,
                )
                existing_price = (await session.execute(p_stmt)).scalar_one_or_none()
                if existing_price:
                    existing_price.price = price_val
                    existing_price.source = SourceType.PARSER
                    existing_price.recorded_at = now
                else:
                    new_price = FuelPrice(
                        station_id=station.id,
                        fuel_type=fuel_type_enum,
                        price=price_val,
                        source=SourceType.PARSER,
                        confidence=0.85,
                        is_fresh=True,
                        recorded_at=now,
                    )
                    session.add(new_price)
                    prices_saved += 1

        await commit_or_rollback(session)
        logger.info(f"[MultiGo] Город {city.name}: сохранено {stations_saved} станций, {prices_saved} цен")
        return stations_saved, prices_saved

    async def sync_all_cities(self, session_factory):
        """Синхронизация всех активных городов"""
        async with session_factory() as session:
            cities_res = await session.execute(select(City).where(City.is_active == True))
            cities = cities_res.scalars().all()

        total_stations = 0
        total_prices = 0
        for idx, city in enumerate(cities, 1):
            logger.info(f"[{idx}/{len(cities)}] Синхронизация {city.name}...")
            async with session_factory() as session:
                added_st, added_pr = await self.sync_city(session, city)
                total_stations += added_st
                total_prices += added_pr
                await asyncio.sleep(1.5)

        logger.info(f"[MultiGo] Синхронизация завершена: станций {total_stations}, цен {total_prices}")


multigo_v2 = MultiGoV2Service()
