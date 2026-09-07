# services/multigo_v2.py — расширенная версия со всеми городами-миллионниками и регионами РФ
import asyncio
import logging
import re
from typing import Dict, Any, List, Optional, Tuple
import aiohttp
from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import City, Station, FuelPrice, FuelType, SourceType
from database.crud import commit_or_rollback
from services.data_sanitizer import clean_brand_name

logger = logging.getLogger(__name__)

NEAR_URL = "https://multigo.ru/api/9/near/list"
PRICES_URL = "https://multigo.ru/api/9/avgprices"

# Прямой маппинг кодов MultiGo в Enum модели (исключает конфликт кириллицы/латиницы)
FUEL_CODES: Dict[str, FuelType] = {
    "8": FuelType.AI_92,    # АИ-92
    "9": FuelType.AI_92,    # АИ-92+
    "11": FuelType.AI_95,   # АИ-95
    "12": FuelType.AI_95,   # АИ-95+
    "14": FuelType.AI_98,   # АИ-98
    "16": FuelType.AI_100,  # АИ-100
    "3": FuelType.DT,       # ДТ
    "4": FuelType.DT,       # ДТ+
    "17": FuelType.DT,      # ДТ Зимний
}

# ПОЛНЫЙ РЕЕСТР: Все 16 городов-миллионников + все крупные региональные центры РФ
CITY_COORDINATES: Dict[str, Tuple[float, float]] = {
    # 🌟 16 ГОРОДОВ-МИЛЛИОННИКОВ РФ
    "москва": (55.7558, 37.6173),
    "санкт-петербург": (59.9343, 30.3351),
    "новосибирск": (55.0084, 82.9357),
    "екатеринбург": (56.8389, 60.6057),
    "казань": (55.7961, 49.1064),
    "нижний новгород": (56.2965, 43.9361),
    "челябинск": (55.1644, 61.4368),
    "красноярск": (56.0153, 92.8932),
    "самара": (53.1959, 50.1002),
    "уфа": (54.7388, 55.9721),
    "ростов-на-дону": (47.2357, 39.7015),
    "омск": (54.9885, 73.3242),
    "краснодар": (45.0355, 38.9753),
    "воронеж": (51.6608, 39.2003),
    "пермь": (58.0105, 56.2502),
    "волгоград": (48.7080, 44.5133),

    # 🏙️ КРУПНЕЙШИЕ РЕГИОНАЛЬНЫЕ ЦЕНТРЫ (500k+ и ключевые узлы)
    "саратов": (51.5335, 46.0342),
    "тюмень": (57.1530, 65.5343),
    "тольятти": (53.5088, 49.4189),
    "барнаул": (53.3548, 83.7698),
    "ижевск": (56.8528, 53.2115),
    "махачкала": (42.9849, 47.5047),
    "хабаровск": (48.4827, 135.0838),
    "ульяновск": (54.3142, 48.4031),
    "иркутск": (52.2871, 104.2810),
    "владивосток": (43.1155, 131.8855),
    "ярославль": (57.6261, 39.8845),
    "севастополь": (44.6166, 33.5254),
    "томск": (56.4977, 84.9744),
    "ставрополь": (45.0428, 41.9734),
    "кемерово": (55.3547, 86.0872),
    "набережные челны": (55.7437, 52.4098),
    "оренбург": (51.7682, 55.0970),
    "новокузнецк": (53.7596, 87.1216),
    "рязань": (54.6295, 39.7425),
    "балашиха": (55.7963, 37.9382),
    "пенза": (53.1959, 45.0183),
    "чебоксары": (56.1439, 47.2489),
    "липецк": (52.6103, 39.5947),
    "калининград": (54.7104, 20.4522),
    "астрахань": (46.3497, 48.0408),
    "тула": (54.1931, 37.6173),
    "киров": (58.6035, 49.6679),
    "сочи": (43.6028, 39.7342),
    "курск": (51.7304, 36.1927),
    "улан-удэ": (51.8345, 107.5845),
    "тверь": (56.8587, 35.9176),
    "магнитогорск": (53.4186, 58.9759),
    "иваново": (57.0003, 40.9739),
    "брянск": (53.2436, 34.3634),
    "белгород": (50.5954, 36.5873),
    "сургут": (61.2540, 73.4141),
    "владимир": (56.1290, 40.4066),
    "чита": (52.0317, 113.5009),
    "архангельск": (64.5399, 40.5158),
    "симферополь": (44.9521, 34.1024),
    "калуга": (54.5138, 36.2612),
    "смоленск": (54.7826, 32.0453),
    "волжский": (48.7858, 44.7797),
    "якутск": (62.0355, 129.6755),
    "саранск": (54.1874, 45.1839),
    "череповец": (59.1226, 37.9035),
    "курган": (55.4410, 65.3411),
    "вологда": (59.2205, 39.8915),
    "орёл": (52.9685, 36.0694),
    "орел": (52.9685, 36.0694),
    "подольск": (55.4312, 37.5458),
    "грозный": (43.3179, 45.6982),
    "владикавказ": (43.0246, 44.6818),
    "тамбов": (52.7212, 41.4523),
    "мурманск": (68.9707, 33.0750),
    "петрозаводск": (61.7891, 34.3596),
    "нижневартовск": (60.9385, 76.5589),
    "кострома": (57.7679, 40.9269),
    "новороссийск": (44.7239, 37.7687),
    "йошкар-ола": (56.6344, 47.8999),
    "химки": (55.8887, 37.4304),
    "таганрог": (47.2362, 38.8969),
    "сыктывкар": (61.6688, 50.8357),
    "комсомольск-на-амуре": (50.5499, 137.0068),
    "нальчик": (43.4853, 43.6071),
    "нижнекамск": (55.6366, 51.8219),
    "шахты": (47.7086, 40.2157),
    "дзержинск": (56.2389, 43.4631),
    "братск": (56.1522, 101.6142),
    "орск": (51.2049, 58.5668),
    "ангарск": (52.5444, 103.8882),
    "энгельс": (51.4982, 46.1206),
    "благовещенск": (50.2796, 127.5405),
    "старый оскол": (51.2975, 37.8350),
    "великий новгород": (58.5215, 31.2755),
    "королёв": (55.9142, 37.8249),
    "королев": (55.9142, 37.8249),
    "псков": (57.8193, 28.3318),
    "мытищи": (55.9116, 37.7308),
    "бийск": (52.5300, 85.1700),
    "люберцы": (55.6772, 37.8932),
    "прокопьевск": (53.9056, 86.7197),
    "южно-сахалинск": (46.9591, 142.7380),
    "армавир": (44.9892, 41.1219),
    "рыбинск": (58.0484, 38.8584),
    "абакан": (53.7212, 91.4424),
    "петропавловск-камчатский": (53.0440, 158.6508),
    "норильск": (69.3535, 88.2027),
    "уссурийск": (43.7972, 131.9520),
    "волгодонск": (47.5146, 42.1530),
    "новочеркасск": (47.4221, 40.0934),
    "сызрань": (53.1558, 48.4682),
    "каменск-уральский": (56.4185, 61.9328),
    "златоуст": (55.1711, 59.6509),
    "альметьевск": (54.9014, 52.2974),
    "электросталь": (55.7928, 38.4419),
    "салават": (53.3619, 55.9244),
    "миасс": (55.0573, 60.1083),
    "находка": (42.8239, 132.8797),
    "копейск": (55.1169, 61.6186),
    "пятигорск": (44.0500, 43.0500),
    "рубцовск": (51.5000, 81.2000),
    "кисловодск": (43.9133, 42.7200),
    "майкоп": (44.6089, 40.1058),
    "черкесск": (44.2233, 42.0578),
    "элиста": (46.3078, 44.2558),
    "горно-алтайск": (51.9581, 85.9603),
    "кызыл": (51.7184, 94.4435),
    "магадан": (59.5638, 150.8037),
    "анадырь": (64.7335, 177.5097),
    "салехард": (66.5299, 66.6145),
    "ханты-мансийск": (61.0032, 69.0186),
}

BRAND_MODIFIERS = {
    "лукойл": 1.012,
    "lukoil": 1.012,
    "газпромнефть": 1.010,
    "gazpromneft": 1.010,
    "teboil": 1.008,
    "тебойл": 1.008,
    "роснефть": 1.000,
    "rosneft": 1.000,
    "татнефть": 0.995,
    "tatneft": 0.995,
    "башнефть": 0.990,
    "bashneft": 0.990,
    "нефтьмагистраль": 1.006,
    "трасса": 1.005,
    "irbis": 1.004,
    "независимая": 0.985,
}

def normalize_city_name(raw_name: str) -> Optional[str]:
    if not raw_name:
        return None
    name = raw_name.strip()
    if any(bad in name.lower() for bad in ["цены", "бензин", "дизель", "аи-", "price", "fuel"]):
        return None
    name = re.sub(r"^[\s\-\–\—\.]+", "", name).strip()
    return name if len(name) >= 2 else None

class MultiGoV2Service:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=20, connect=8)
        self.session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                    "Content-Type": "application/json",
                }
            )
        return self.session

    async def fetch_prices(self, lat: float, lon: float) -> Dict[FuelType, Dict[str, float]]:
        payload = {"lat": lat, "lng": lon}
        result: Dict[FuelType, Dict[str, float]] = {}
        try:
            session = await self._get_session()
            async with session.post(PRICES_URL, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    avg = data.get("data", {}).get("avgprice", {})
                    for code, info in avg.items():
                        fuel_enum = FUEL_CODES.get(str(code))
                        if fuel_enum:
                            val = info.get("avg", 0)
                            if val and float(val) > 0:
                                result[fuel_enum] = {
                                    "price": round(float(val), 2),
                                    "delta": round(float(info.get("delta", 0)), 2),
                                }
                    logger.info(f"[MultiGo V2] Успешно получены цены для {len(result)} типов топлива")
                else:
                    logger.warning(f"[MultiGo V2] HTTP {resp.status} при запросе цен")
        except Exception as e:
            logger.error(f"[MultiGo V2] Ошибка получения цен: {e}")
        return result

    async def fetch_stations(self, lat: float, lon: float, limit: int = 1500) -> List[Dict[str, Any]]:
        payload = {"lat": lat, "lng": lon, "limit": limit}
        stations = []
        try:
            session = await self._get_session()
            async with session.post(NEAR_URL, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("data", {}).get("list", [])
                    for it in items:
                        name = it.get("name") or "АЗС"
                        sub = (it.get("subCategory") or {}).get("idx")
                        if sub == 9810 or "элзс" in name.lower():
                            continue
                        loc = it.get("loc")
                        if not loc or len(loc) < 2:
                            continue
                        brand_obj = it.get("brand")
                        brand = brand_obj.get("name") if isinstance(brand_obj, dict) else None
                        if not brand:
                            brand = "Независимая АЗС"
                        address = it.get("address") or f"Координаты: {loc[0]:.4f}, {loc[1]:.4f}"
                        stations.append({
                            "id": str(it.get("id")),
                            "name": name,
                            "brand": brand,
                            "address": address,
                            "lat": float(loc[0]),
                            "lon": float(loc[1]),
                        })
                    logger.info(f"[MultiGo V2] Получено {len(stations)} АЗС вокруг ({lat}, {lon})")
                else:
                    logger.warning(f"[MultiGo V2] HTTP {resp.status} при запросе станций")
        except Exception as e:
            logger.error(f"[MultiGo V2] Ошибка получения станций: {e}")
        return stations

    async def sync_city(self, session: AsyncSession, city_name: str) -> Tuple[int, int]:
        clean_name = normalize_city_name(city_name)
        if not clean_name:
            return 0, 0

        # 1. Ищем город в базе данных
        city_stmt = select(City).where(func.lower(City.name) == clean_name.lower(), City.is_active == True)
        city_res = await session.execute(city_stmt)
        city = city_res.scalar_one_or_none()
        if not city:
            logger.warning(f"[MultiGo V2] Город {city_name} не найден в БД, пропускаем")
            return 0, 0

        # 2. Определение координат: сначала словарь, если нет — координаты из модели City в БД
        coords = CITY_COORDINATES.get(clean_name.lower())
        if not coords and city.latitude and city.longitude:
            coords = (float(city.latitude), float(city.longitude))

        if not coords:
            logger.warning(f"[MultiGo V2] Город {city_name} не имеет координат ни в словаре, ни в БД")
            return 0, 0

        lat, lon = coords

        # 3. Запрос цен и станций
        prices = await self.fetch_prices(lat, lon)
        stations = await self.fetch_stations(lat, lon)
        if not stations:
            logger.warning(f"[MultiGo V2] Не удалось получить станции для {city_name}")
            return 0, 0

        city_id = city.id
        stations_added = 0
        prices_added = 0

        stmt = select(Station).where(Station.city_id == city_id, Station.is_active == True)
        existing = (await session.execute(stmt)).scalars().all()
        coord_index = {
            (round(s.latitude, 3), round(s.longitude, 3)): s
            for s in existing if s.latitude and s.longitude
        }

        for st_data in stations:
            lat_s = st_data["lat"]
            lon_s = st_data["lon"]
            key = (round(lat_s, 3), round(lon_s, 3))

            station = coord_index.get(key)
            if not station:
                brand = clean_brand_name(st_data["brand"])
                station = Station(
                    city_id=city_id,
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
                stations_added += 1
            else:
                if (not station.address or "уточн" in station.address.lower()) and st_data["address"]:
                    station.address = st_data["address"][:300]
                    await session.flush()

        if prices:
            now = datetime.now(timezone.utc)
            for st in coord_index.values():
                brand_key = (st.brand or "").lower()
                mod = 1.0
                for b_name, b_val in BRAND_MODIFIERS.items():
                    if b_name in brand_key:
                        mod = b_val
                        break

                for fuel_type_enum, p_info in prices.items():
                    price_val = round(p_info["price"] * mod, 2)
                    if price_val < 30 or price_val > 150:
                        continue

                    p_stmt = select(FuelPrice).where(
                        FuelPrice.station_id == st.id,
                        FuelPrice.fuel_type == fuel_type_enum,
                        FuelPrice.is_fresh == True,
                    )
                    existing_price = (await session.execute(p_stmt)).scalar_one_or_none()
                    if existing_price:
                        existing_price.price = price_val
                        existing_price.source = SourceType.PARSER
                        existing_price.recorded_at = now
                        prices_added += 1
                    else:
                        new_price = FuelPrice(
                            station_id=st.id,
                            fuel_type=fuel_type_enum,
                            price=price_val,
                            source=SourceType.PARSER,
                            confidence=0.85,
                            is_fresh=True,
                            recorded_at=now,
                        )
                        session.add(new_price)
                        prices_added += 1

        await commit_or_rollback(session)
        logger.info(f"[MultiGo V2] Город {city_name}: сохранено {stations_added} станций, {prices_added} цен")
        return stations_added, prices_added

    async def sync_all_cities(self, session_factory):
        async with session_factory() as session:
            cities_res = await session.execute(select(City).where(City.is_active == True))
            cities = cities_res.scalars().all()

        total_stations = 0
        total_prices = 0
        for idx, city in enumerate(cities, 1):
            logger.info(f"[{idx}/{len(cities)}] Синхронизация {city.name}...")
            try:
                async with session_factory() as session:
                    added_st, added_pr = await self.sync_city(session, city.name)
                    total_stations += added_st
                    total_prices += added_pr
            except Exception as e:
                logger.error(f"Ошибка при синхронизации города {city.name}: {e}")
            await asyncio.sleep(1.5)

        logger.info(f"[MultiGo V2] Синхронизация завершена: станций {total_stations}, цен {total_prices}")

multigo_v2 = MultiGoV2Service()
