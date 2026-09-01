import asyncio
import logging
from typing import Optional, Dict, Any
import aiohttp
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

class GdebenzParser:
    BASE_URL = "https://gdebenz.ru/api/stations"
    
    @staticmethod
    async def fetch_city_stations(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> Optional[Dict[str, Any]]:
        params = {
            "lat_min": lat_min,
            "lat_max": lat_max,
            "lon_min": lon_min,
            "lon_max": lon_max,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        timeout = aiohttp.ClientTimeout(total=7.0, connect=3.0)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(GdebenzParser.BASE_URL, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    logger.warning(f"[DataCollector] gdebenz HTTP {resp.status}")
                    return None
        except asyncio.TimeoutError:
            logger.warning("[DataCollector] gdebenz таймаут ответа")
            return None
        except Exception as e:
            logger.error(f"[DataCollector] Ошибка сети gdebenz: {e}")
            return None

    @staticmethod
    async def save_stations_to_db(stations_data: Dict[str, Any]):
        if not stations_data or "stations" not in stations_data:
            return
        stations = stations_data.get("stations", [])
        if not stations:
            return

        fuel_type_map = {
            "ai92": "AI-92",
            "ai95": "AI-95",
            "ai98": "AI-98",
            "ai100": "AI-100",
            "dt": "DT",
        }

        async with AsyncSessionLocal() as db:
            try:
                for station in stations:
                    station_id = station.get("id")
                    if not station_id:
                        continue
                    queue_level = station.get("queue_level", "unknown")
                    for fuel_code, fuel_info in station.get("fuels", {}).items():
                        fuel_type = fuel_type_map.get(fuel_code.lower())
                        if not fuel_type:
                            continue
                        price = fuel_info.get("price")
                        available = fuel_info.get("available", False)
                        availability = "available" if available else "unavailable"
                        
                        await db.execute(text("""
                            INSERT INTO station_current_fuel
                                (station_id, fuel_type, price, availability, queue_level, observed_at, source, confidence)
                            VALUES
                                (:station_id, :fuel_type, :price, :availability, :queue_level, NOW(), 'gdebenz', 0.8)
                            ON CONFLICT (station_id, fuel_type) DO UPDATE SET
                                price = COALESCE(EXCLUDED.price, station_current_fuel.price),
                                availability = EXCLUDED.availability,
                                queue_level = EXCLUDED.queue_level,
                                observed_at = NOW(),
                                source = EXCLUDED.source,
                                confidence = EXCLUDED.confidence;
                        """), {
                            "station_id": station_id,
                            "fuel_type": fuel_type,
                            "price": price,
                            "availability": availability,
                            "queue_level": queue_level,
                        })
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error(f"[DataCollector] Ошибка записи в БД: {e}")

async def data_collector_worker():
    """Фоновый воркер: опрашивает API раз в 15 минут."""
    logger.info("[DataCollector] Воркер запущен")
    await asyncio.sleep(10)  # даём боту время на инициализацию
    
    while True:
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(text("SELECT id, name, latitude, longitude FROM cities WHERE is_active = True LIMIT 20"))
                cities = result.mappings().all()

            for city in cities:
                lat = float(city["latitude"])
                lon = float(city["longitude"])
                data = await GdebenzParser.fetch_city_stations(lat - 0.4, lat + 0.4, lon - 0.4, lon + 0.4)
                if data:
                    await GdebenzParser.save_stations_to_db(data)
                await asyncio.sleep(2)  # пауза между городами
        except asyncio.CancelledError:
            logger.info("[DataCollector] Воркер остановлен")
            break
        except Exception as e:
            logger.error(f"[DataCollector] Ошибка в итерации: {e}")
        await asyncio.sleep(900)  # 15 минут
