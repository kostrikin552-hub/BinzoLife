# services/multigo_v2.py — финальная версия для PostgreSQL + SQLAlchemy (async)
import asyncio
import logging
import re
from typing import Dict, Any, List, Optional, Tuple
import aiohttp
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import City, Station, FuelPrice, FuelType, SourceType
from database.crud import commit_or_rollback
from services.data_sanitizer import clean_brand_name

logger = logging.getLogger(__name__)

# Константы MultiGo API
NEAR_URL = "https://multigo.ru/api/9/near/list"
PRICES_URL = "https://multigo.ru/api/9/avgprices"

# Полный справочник координат (100+ городов РФ + транслит)
CITY_COORDINATES = {
    # Топ мегаполисы
    "москва": (55.7558, 37.6173),
    "moscow": (55.7558, 37.6173),
    "санкт-петербург": (59.9343, 30.3351),
    "saint petersburg": (59.9343, 30.3351),
    "новосибирск": (55.0084, 82.9357),
    "novosibirsk": (55.0084, 82.9357),
    "екатеринбург": (56.8389, 60.6057),
    "ekaterinburg": (56.8389, 60.6057),
    "казань": (55.7961, 49.1064),
    "kazan": (55.7961, 49.1064),
    "нижний новгород": (56.2965, 43.9361),
    "nizhny novgorod": (56.2965, 43.9361),
    "челябинск": (55.1644, 61.4368),
    "chelyabinsk": (55.1644, 61.4368),
    "самара": (53.1959, 50.1002),
    "samara": (53.1959, 50.1002),
    "омск": (54.9885, 73.3242),
    "omsk": (54.9885, 73.3242),
    "ростов-на-дону": (47.2357, 39.7015),
    "rostov-on-don": (47.2357, 39.7015),
    "уфа": (54.7388, 55.9721),
    "ufa": (54.7388, 55.9721),
    "красноярск": (56.0153, 92.8932),
    "krasnoyarsk": (56.0153, 92.8932),
    "воронеж": (51.6608, 39.2003),
    "voronezh": (51.6608, 39.2003),
    "пермь": (58.0105, 56.2502),
    "perm": (58.0105, 56.2502),
    "волгоград": (48.7080, 44.5133),
    "volgograd": (48.7080, 44.5133),
    # Города из вашего лога (ранее пропускавшиеся)
    "иваново": (57.0003, 40.9739),
    "ivanovo": (57.0003, 40.9739),
    "брянск": (53.2436, 34.3634),
    "bryansk": (53.2436, 34.3634),
    "белгород": (50.5954, 36.5873),
    "belgorod": (50.5954, 36.5873),
    "сургут": (61.2540, 73.4141),
    "surgut": (61.2540, 73.4141),
    "владимир": (56.1290, 40.4066),
    "vladimir": (56.1290, 40.4066),
    "чита": (52.0317, 113.5009),
    "chita": (52.0317, 113.5009),
    "архангельск": (64.5399, 40.5158),
    "arkhangelsk": (64.5399, 40.5158),
    "симферополь": (44.9521, 34.1024),
    "simferopol": (44.9521, 34.1024),
    "калуга": (54.5138, 36.2612),
    "kaluga": (54.5138, 36.2612),
    "смоленск": (54.7826, 32.0453),
    "smolensk": (54.7826, 32.0453),
    "вологда": (59.2205, 39.8915),
    "vologda": (59.2205, 39.8915),
    "саранск": (54.1874, 45.1839),
    "saransk": (54.1874, 45.1839),
    "череповец": (59.1226, 37.9035),
    "cherepovets": (59.1226, 37.9035),
    "курган": (55.4410, 65.3411),
    "kurgan": (55.4410, 65.3411),
    "орёл": (52.9685, 36.0694),
    "орел": (52.9685, 36.0694),
    "orel": (52.9685, 36.0694),
    "владикавказ": (43.0246, 44.6818),
    "vladikavkaz": (43.0246, 44.6818),
    "мурманск": (68.9707, 33.0750),
    "murmansk": (68.9707, 33.0750),
    "тамбов": (52.7212, 41.4523),
    "tambov": (52.7212, 41.4523),
    "петрозаводск": (61.7891, 34.3596),
    "petrozavodsk": (61.7891, 34.3596),
    "кострома": (57.7679, 40.9269),
    "kostroma": (57.7679, 40.9269),
    "новороссийск": (44.7239, 37.7687),
    "novorossiysk": (44.7239, 37.7687),
    "йошкар-ола": (56.6344, 47.8999),
    "yoshkar-ola": (56.6344, 47.8999),
    "элиста": (46.3078, 44.2558),
    "elista": (46.3078, 44.2558),
    "грозный": (43.3179, 45.6982),
    "grozny": (43.3179, 45.6982),
    "нальчик": (43.4853, 43.6071),
    "nalchik": (43.4853, 43.6071),
    "майкоп": (44.6089, 40.1058),
    "maykop": (44.6089, 40.1058),
    "черкесск": (44.2233, 42.0578),
    "cherkessk": (44.2233, 42.0578),
    "абакан": (53.7212, 91.4424),
    "abakan": (53.7212, 91.4424),
    "горно-алтайск": (51.9581, 85.9603),
    "gorno-altaysk": (51.9581, 85.9603),
    # Сибирь, Юг, Поволжье
    "краснодар": (45.0355, 38.9753),
    "krasnodar": (45.0355, 38.9753),
    "тюмень": (57.1530, 65.5343),
    "tyumen": (57.1530, 65.5343),
    "тольятти": (53.5088, 49.4189),
    "tolyatti": (53.5088, 49.4189),
    "барнаул": (53.3548, 83.7698),
    "barnaul": (53.3548, 83.7698),
    "ижевск": (56.8528, 53.2115),
    "izhevsk": (56.8528, 53.2115),
    "ульяновск": (54.3142, 48.4031),
    "ulyanovsk": (54.3142, 48.4031),
    "иркутск": (52.2871, 104.2810),
    "irkutsk": (52.2871, 104.2810),
    "владивосток": (43.1155, 131.8855),
    "vladivostok": (43.1155, 131.8855),
    "ярославль": (57.6261, 39.8845),
    "yaroslavl": (57.6261, 39.8845),
    "кемерово": (55.3547, 86.0872),
    "kemerovo": (55.3547, 86.0872),
    "томск": (56.4977, 84.9744),
    "tomsk": (56.4977, 84.9744),
    "набережные челны": (55.7437, 52.4098),
    "naberezhnye chelny": (55.7437, 52.4098),
    "оренбург": (51.7682, 55.0970),
    "orenburg": (51.7682, 55.0970),
    "рязань": (54.6295, 39.7425),
    "ryazan": (54.6295, 39.7425),
    "пенза": (53.1959, 45.0183),
    "penza": (53.1959, 45.0183),
    "кызыл": (51.7184, 94.4435),
    "kyzyl": (51.7184, 94.4435),
    "бийск": (52.5300, 85.1700),
    "biysk": (52.5300, 85.1700),
    "рубцовск": (51.5000, 81.2000),
    "rubtsovsk": (51.5000, 81.2000),
    "кисловодск": (43.9133, 42.7200),
    "kislovodsk": (43.9133, 42.7200),
    "пятигорск": (44.0500, 43.0500),
    "pyatigorsk": (44.0500, 43.0500),
    "ессентуки": (44.0500, 42.8500),
    "yessentuki": (44.0500, 42.8500),
    "минеральные воды": (44.2000, 43.1333),
    "mineralnye vody": (44.2000, 43.1333),
    "сочи": (43.6028, 39.7342),
    "sochi": (43.6028, 39.7342),
    "ставрополь": (45.0428, 41.9734),
    "stavropol": (45.0428, 41.9734),
    "хабаровск": (48.4827, 135.0838),
    "khabarovsk": (48.4827, 135.0838),
    "махачкала": (42.9849, 47.5047),
    "makhachkala": (42.9849, 47.5047),
    "новокузнецк": (53.7596, 87.1216),
    "novokuznetsk": (53.7596, 87.1216),
}

# Маппинг кодов топлива
FUEL_CODES = {
    "1": "СУГ",
    "3": "ДТ",
    "4": "ДТ+",
    "8": "АИ-92",
    "9": "АИ-92+",
    "11": "АИ-95",
    "12": "АИ-95+",
    "14": "АИ-98",
    "16": "АИ-100",
    "17": "ДТ Зимний",
    "18": "КПГ",
}

# Брендовые модификаторы (расширенные)
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
    """Фильтрует мусорные строки вида '- цены на бензин АИ-92'"""
    if not raw_name:
        return None
    name = raw_name.strip()
    # Исключаем строки-заголовки
    if any(bad in name.lower() for bad in ["цены", "бензин", "дизель", "аи-", "price", "fuel"]):
        return None
    name = re.sub(r"^[\s\-\–\—\.]+", "", name).strip()
    return name if len(name) >= 2 else None


def get_coords(city_name: str) -> Optional[Tuple[float, float]]:
    """Возвращает координаты по названию города (с нормализацией)"""
    cleaned = normalize_city_name(city_name)
    if not cleaned:
        return None
    return CITY_COORDINATES.get(cleaned.lower())


class MultiGoV2Service:
    """Сервис синхронизации через MultiGo V2 (адаптирован под PostgreSQL async)"""

    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=15, connect=8)
        self.session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Content-Type": "application/json",
                }
            )
        return self.session

    async def fetch_prices(self, lat: float, lon: float) -> Dict[str, Dict[str, float]]:
        """Получает цены региона через /avgprices"""
        payload = {"lat": lat, "lng": lon}
        result = {}
        try:
            session = await self._get_session()
            async with session.post(PRICES_URL, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    avg = data.get("data", {}).get("avgprice", {})
                    for code, info in avg.items():
                        if code in FUEL_CODES:
                            val = info.get("avg", 0)
                            if val > 0:
                                result[FUEL_CODES[code]] = {
                                    "price": round(val, 2),
                                    "delta": round(info.get("delta", 0), 2),
                                    "sample_size": info.get("cnt", 0),
                                }
                    logger.info(f"[MultiGo V2] Получены цены для {len(result)} видов топлива")
                else:
                    logger.warning(f"[MultiGo V2] HTTP {resp.status} при запросе цен")
        except Exception as e:
            logger.error(f"[MultiGo V2] Ошибка получения цен: {e}")
        return result

    async def fetch_stations(self, lat: float, lon: float, limit: int = 60) -> List[Dict[str, Any]]:
        """Получает список АЗС через /near/list (исключая ЭлЗС)"""
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
                        # Безопасное извлечение бренда
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
        """
        Синхронизирует город: станции + цены.
        Возвращает (количество станций, количество цен).
        """
        coords = get_coords(city_name)
        if not coords:
            logger.warning(f"[MultiGo V2] Город {city_name} без координат, пропускаем")
            return 0, 0

        lat, lon = coords

        # 1. Получаем цены
        prices = await self.fetch_prices(lat, lon)
        if not prices:
            logger.warning(f"[MultiGo V2] Не удалось получить цены для {city_name}")

        # 2. Получаем станции
        stations = await self.fetch_stations(lat, lon)
        if not stations:
            logger.warning(f"[MultiGo V2] Не удалось получить станции для {city_name}")
            return 0, 0

        # 3. Сохраняем станции и цены в БД
        stations_added = 0
        prices_added = 0

        # Индекс существующих станций по координатам
        stmt = select(Station).where(Station.city_id == 0, Station.is_active == True)
        # Здесь нужно найти city_id по названию города
        city_stmt = select(City).where(City.name == city_name, City.is_active == True)
        city_res = await session.execute(city_stmt)
        city = city_res.scalar_one_or_none()
        if not city:
            logger.warning(f"[MultiGo V2] Город {city_name} не найден в БД, пропускаем")
            return 0, 0

        city_id = city.id

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
                # Обновляем адрес, если он пустой
                if (not station.address or station.address == "" or "уточн" in station.address.lower()) and st_data["address"]:
                    station.address = st_data["address"][:300]
                    await session.flush()

        # Привязываем цены к станциям с учётом брендового модификатора
        if prices:
            now = datetime.now(timezone.utc)
            for st in coord_index.values():
                # Определяем модификатор для бренда
                brand_key = st.brand.lower() if st.brand else ""
                mod = 1.0
                for b_name, b_val in BRAND_MODIFIERS.items():
                    if b_name in brand_key:
                        mod = b_val
                        break

                for fuel_name, p_info in prices.items():
                    # Проверяем, есть ли такой тип топлива в FuelType
                    fuel_type_enum = None
                    for ft in FuelType:
                        if ft.value == fuel_name:
                            fuel_type_enum = ft
                            break
                    if not fuel_type_enum:
                        continue

                    price_val = round(p_info["price"] * mod, 2)
                    if price_val < 30 or price_val > 150:
                        continue  # защита от выбросов

                    # Обновляем или вставляем цену
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
        logger.info(f"[MultiGo V2] Город {city_name}: добавлено {stations_added} станций, {prices_added} цен")
        return stations_added, prices_added

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
                added_st, added_pr = await self.sync_city(session, city.name)
                total_stations += added_st
                total_prices += added_pr
                await asyncio.sleep(1.5)  # пауза между городами

        logger.info(f"[MultiGo V2] Синхронизация завершена: станций {total_stations}, цен {total_prices}")

multigo_v2 = MultiGoV2Service()
