# services/multigo_v2.py
import asyncio
import json
import logging
from typing import List, Dict, Optional, Tuple
import aiohttp
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Константы MultiGo V2 API
NEAR_LIST_URL = "https://multigo.ru/api/9/near/list"
AVG_PRICES_URL = "https://multigo.ru/api/9/avgprices"

# Маппинг кодов топлива в читаемые названия (соответствует FuelType enum)
FUEL_CODES = {
    "1": "СУГ",          # пропан-бутан
    "3": "ДТ",           # дизельное топливо
    "4": "ДТ+",          # дизель премиум
    "8": "АИ-92",        # АИ-92
    "9": "АИ-92+",       # АИ-92 улучшенный
    "11": "АИ-95",       # АИ-95
    "12": "АИ-95+",      # АИ-95 улучшенный (G-Drive / ЭКТО)
    "14": "АИ-98",       # АИ-98
    "16": "АИ-100",      # АИ-100 Racing
    "17": "ДТ Зимний",   # дизель зимний
    "18": "КПГ",         # метан сжатый
}

# Коэффициенты для разных брендов (относительно средней цены)
BRAND_PRICE_MODIFIERS = {
    "ЛУКОЙЛ": 1.012,         # +1.2%
    "ГАЗПРОМНЕФТЬ": 1.010,   # +1.0%
    "TEBOIL": 1.008,         # +0.8%
    "РОСНЕФТЬ": 1.000,       # базовая
    "ТАТНЕФТЬ": 0.995,       # -0.5%
    "БАШНЕФТЬ": 0.990,       # -1.0%
    "Нефтьмагистраль": 1.006,
    "ТРАССА": 1.005,
    "Irbis": 1.004,
    "Независимая АЗС": 0.985,
}

class MultiGoV2Collector:
    """
    Сборщик АЗС и цен через MultiGo API версии 9.
    Не требует подписи и сложных заголовков.
    """

    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=20, connect=10)
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

    async def fetch_stations_near(self, lat: float, lon: float, limit: int = 60) -> List[Dict]:
        """
        Получает список АЗС в радиусе 30 км от указанных координат.
        Возвращает список словарей с полями: id, name, brand, address, lat, lon.
        """
        payload = {"lat": lat, "lng": lon, "limit": limit}
        stations = []
        try:
            session = await self._get_session()
            async with session.post(NEAR_LIST_URL, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("data", {}).get("list", [])
                    for it in items:
                        name = it.get("name", "")
                        sub_idx = it.get("subCategory", {}).get("idx")
                        # Исключаем зарядные станции для электромобилей (ЭлЗС)
                        if sub_idx == 9810 or "элзс" in name.lower():
                            continue
                        brand_info = it.get("brand") or {}
                        brand_name = brand_info.get("name") or "Независимая АЗС"
                        loc = it.get("loc")
                        if not loc or len(loc) < 2:
                            continue
                        address = it.get("address") or f"Координаты: {loc[0]:.4f}, {loc[1]:.4f}"
                        stations.append({
                            "id": str(it.get("id")),
                            "name": name,
                            "brand": brand_name.upper(),
                            "address": address,
                            "latitude": loc[0],
                            "longitude": loc[1],
                            "sub_category": it.get("subCategory", {}).get("name", "АЗС"),
                            "raw": it,
                        })
                    logger.info(f"[MultiGo V2] Получено {len(stations)} АЗС вокруг ({lat}, {lon})")
                else:
                    logger.warning(f"[MultiGo V2] HTTP {resp.status} при запросе станций")
        except Exception as e:
            logger.error(f"[MultiGo V2] Ошибка получения станций: {e}")
        return stations

    async def fetch_avg_prices(self, lat: float, lon: float) -> Optional[Dict[str, Dict]]:
        """
        Получает средние цены на топливо в регионе по координатам.
        Возвращает словарь вида:
        {
            "АИ-95": {"price": 55.80, "delta": 0.15, "sample_size": 120},
            ...
        }
        """
        payload = {"lat": lat, "lng": lon}
        result = {}
        try:
            session = await self._get_session()
            async with session.post(AVG_PRICES_URL, json=payload) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    avg_prices = data.get("data", {}).get("avgprice", {})
                    for code, info in avg_prices.items():
                        if code in FUEL_CODES:
                            fuel_name = FUEL_CODES[code]
                            avg_val = info.get("avg", 0)
                            if avg_val > 0:
                                result[fuel_name] = {
                                    "price": round(avg_val, 2),
                                    "delta": round(info.get("delta", 0), 2),
                                    "sample_size": info.get("cnt", 0),
                                }
                    logger.info(f"[MultiGo V2] Получены цены для {len(result)} видов топлива")
                else:
                    logger.warning(f"[MultiGo V2] HTTP {resp.status} при запросе цен")
        except Exception as e:
            logger.error(f"[MultiGo V2] Ошибка получения цен: {e}")
        return result

    async def sync_city(
        self,
        session,
        city_id: int,
        city_name: str,
        lat: float,
        lon: float,
        limit: int = 60,
    ) -> Tuple[int, int]:
        """
        Полная синхронизация города: станции + цены.
        Возвращает (количество станций, количество цен).
        """
        stations = await self.fetch_stations_near(lat, lon, limit)
        if not stations:
            return 0, 0

        prices = await self.fetch_avg_prices(lat, lon)
        if not prices:
            # Если цены не получены, всё равно сохраняем станции (без цен)
            prices = {}

        from database.crud import sync_multigo_v2_stations
        added, updated = await sync_multigo_v2_stations(
            session,
            city_id=city_id,
            city_name=city_name,
            stations_data=stations,
            prices_data=prices,
        )
        return added, updated

    async def sync_all_cities(self, session_factory):
        """
        Обход всех активных городов с синхронизацией.
        """
        from database.models import City
        from sqlalchemy import select

        async with session_factory() as session:
            cities_res = await session.execute(select(City).where(City.is_active == True))
            cities = cities_res.scalars().all()

        total_stations = 0
        total_prices = 0
        for idx, city in enumerate(cities, 1):
            if not city.latitude or not city.longitude:
                logger.warning(f"[MultiGo V2] Город {city.name} без координат, пропускаем")
                continue
            logger.info(f"[{idx}/{len(cities)}] Синхронизация {city.name}...")
            async with session_factory() as session:
                added, updated = await self.sync_city(
                    session,
                    city.id,
                    city.name,
                    city.latitude,
                    city.longitude,
                )
                total_stations += added
                total_prices += updated
                await asyncio.sleep(1.5)  # пауза между городами
        logger.info(f"[MultiGo V2] Синхронизация завершена: станций {total_stations}, цен {total_prices}")

multigo_v2 = MultiGoV2Collector()
