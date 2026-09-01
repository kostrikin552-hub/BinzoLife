import aiohttp
import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple
from sqlalchemy import text, select
from database.session import AsyncSessionLocal
from database.models import Station, City
from database.crud import get_city_by_name

logger = logging.getLogger(__name__)

# ========== КОНФИГИ ==========
GDEBENZ_API_URL = "https://gdebenz.ru/api/stations"
# Benzuber API (нужен ключ, бесплатный при регистрации)
BENZUBER_API_URL = "https://api.benzuber.ru/v1"
BENZUBER_API_KEY = None  # Задать из переменных окружения позже

# Карта топлива
FUEL_TYPE_MAP = {
    "ai92": "AI-92",
    "ai95": "AI-95",
    "ai98": "AI-98",
    "ai100": "AI-100",
    "dt": "DT",
    "dizel": "DT",
}
# =============================

async def get_or_create_station(db, lat: float, lon: float, brand: str = None, name: str = None, address: str = None) -> int:
    """
    Находит станцию по координатам и бренду. Если не находит, создаёт новую.
    Возвращает station_id.
    """
    tolerance = 0.001  # ~100 метров
    query = select(Station).where(
        Station.latitude.between(lat - tolerance, lat + tolerance),
        Station.longitude.between(lon - tolerance, lon + tolerance)
    )
    if brand:
        query = query.where(Station.brand == brand)
    result = await db.execute(query)
    station = result.scalar_one_or_none()

    if station:
        return station.id

    # Если не нашли — создаём новую
    if not name:
        name = f"АЗС {brand or 'неизвестная'}"
    if not address:
        address = ""

    # Определяем город по координатам (упрощённо: берём ближайший город из таблицы)
    city_id = await get_nearest_city(db, lat, lon)
    if not city_id:
        city_id = 1  # fallback

    new_station = Station(
        name=name,
        brand=brand,
        address=address,
        latitude=lat,
        longitude=lon,
        is_active=True,
        city_id=city_id
    )
    db.add(new_station)
    await db.flush()
    await db.commit()
    logger.info(f"Создана новая станция {new_station.id} ({name})")
    return new_station.id

async def get_nearest_city(db, lat: float, lon: float) -> Optional[int]:
    """Находит ближайший город по координатам (приблизительно)."""
    # Упрощённо: берём город с минимальным расстоянием (не идеально, но для старта достаточно)
    cities = await db.execute(select(City).where(City.is_active == True))
    cities = cities.scalars().all()
    if not cities:
        return None
    from utils.helpers import haversine_distance
    nearest = min(cities, key=lambda c: haversine_distance(lat, lon, c.latitude, c.longitude))
    return nearest.id

async def fetch_gdebenz_stations(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> Optional[List[Dict]]:
    """Запрашивает данные с gdebenz.ru в bounding box."""
    params = {
        "lat_min": lat_min,
        "lat_max": lat_max,
        "lon_min": lon_min,
        "lon_max": lon_max,
    }
    headers = {
        "User-Agent": "BinzoLifeBot/1.0 (https://t.me/BinzoLife_bot)"
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(GDEBENZ_API_URL, params=params, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("stations", [])
                else:
                    logger.warning(f"gdebenz вернул {resp.status}")
                    return None
    except Exception as e:
        logger.error(f"Ошибка gdebenz: {e}")
        return None

async def save_gdebenz_stations(stations: List[Dict]):
    """Сохраняет данные из gdebenz в station_current_fuel и создаёт станции."""
    if not stations:
        return
    async with AsyncSessionLocal() as db:
        for station in stations:
            lat = station.get("lat")
            lon = station.get("lon")
            brand = station.get("brand")
            name = station.get("name") or f"АЗС {brand or ''}"
            address = station.get("address", "")
            if not lat or not lon:
                continue
            station_id = await get_or_create_station(db, lat, lon, brand, name, address)

            # Сохраняем внешний ID
            await db.execute(text("""
                INSERT INTO station_external_ids (station_id, source, external_id)
                VALUES (:station_id, 'gdebenz', :external_id)
                ON CONFLICT (station_id, source) DO UPDATE SET external_id = EXCLUDED.external_id;
            """), {"station_id": station_id, "external_id": str(station.get("id"))})

            # Обрабатываем топливо
            fuels = station.get("fuels", {})
            queue_level = station.get("queue_level", "unknown")
            for fuel_code, fuel_info in fuels.items():
                fuel_type = FUEL_TYPE_MAP.get(fuel_code)
                if not fuel_type:
                    continue
                price = fuel_info.get("price")
                available = fuel_info.get("available", False)
                availability = "available" if available else "unavailable"
                observed_at_str = fuel_info.get("updated_at")
                if observed_at_str:
                    try:
                        observed_at = datetime.fromisoformat(observed_at_str.replace("Z", "+00:00"))
                    except:
                        observed_at = datetime.now(timezone.utc)
                else:
                    observed_at = datetime.now(timezone.utc)

                # Upsert в station_current_fuel
                await db.execute(text("""
                    INSERT INTO station_current_fuel
                        (station_id, fuel_type, price, availability, queue_level, observed_at, source, confidence)
                    VALUES
                        (:station_id, :fuel_type, :price, :availability, :queue_level, :observed_at, 'gdebenz', 0.8)
                    ON CONFLICT (station_id, fuel_type) DO UPDATE SET
                        price = EXCLUDED.price,
                        availability = EXCLUDED.availability,
                        queue_level = EXCLUDED.queue_level,
                        observed_at = EXCLUDED.observed_at,
                        source = EXCLUDED.source,
                        confidence = EXCLUDED.confidence;
                """), {
                    "station_id": station_id,
                    "fuel_type": fuel_type,
                    "price": price,
                    "availability": availability,
                    "queue_level": queue_level,
                    "observed_at": observed_at,
                })
        await db.commit()
        logger.info(f"Обновлено {len(stations)} станций из gdebenz")

async def fetch_benzuber_prices(station_id: int) -> Optional[Dict]:
    """Получает цены и доступность пистолетов через Benzuber API."""
    if not BENZUBER_API_KEY:
        return None
    headers = {"Authorization": f"Bearer {BENZUBER_API_KEY}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{BENZUBER_API_URL}/stations/{station_id}/prices", headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    return None
    except Exception as e:
        logger.error(f"Ошибка Benzuber: {e}")
        return None

async def collect_city_data(city: City):
    """Собирает данные для одного города."""
    lat, lon = city.latitude, city.longitude
    if not lat or not lon:
        return
    # Bounding box ±0.5 градуса (~55 км)
    lat_min, lat_max = lat - 0.5, lat + 0.5
    lon_min, lon_max = lon - 0.5, lon + 0.5

    stations = await fetch_gdebenz_stations(lat_min, lat_max, lon_min, lon_max)
    if stations:
        await save_gdebenz_stations(stations)
    # TODO: добавить Benzuber и сетевые API

async def data_collector_worker(interval_seconds: int = 900):
    """Фоновый сборщик данных, запускается в бесконечном цикле."""
    logger.info("Сборщик данных запущен")
    while True:
        try:
            async with AsyncSessionLocal() as db:
                cities = await db.execute(select(City).where(City.is_active == True))
                cities = cities.scalars().all()
            for city in cities:
                await collect_city_data(city)
            logger.info("Цикл сбора данных завершён")
        except Exception as e:
            logger.error(f"Ошибка в data_collector: {e}", exc_info=True)
        await asyncio.sleep(interval_seconds)
