# services/data_collector.py — исправленная версия (защита от None)
import asyncio
import logging
from typing import Optional, Dict, Any
import aiohttp
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)


class DataCollectorService:
    BASE_URL = "https://gdebenz.ru/api/stations"

    @staticmethod
    async def fetch_city_data(lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> Optional[Dict[str, Any]]:
        params = {
            "lat_min": lat_min,
            "lat_max": lat_max,
            "lon_min": lon_min,
            "lon_max": lon_max,
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
        }
        timeout = aiohttp.ClientTimeout(total=7.0, connect=3.0)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(DataCollectorService.BASE_URL, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
        except Exception as e:
            logger.warning(f"[DataCollector] Сбой запроса к API: {e}")
            return None

    @staticmethod
    async def save_to_db(data: Dict[str, Any]):
        if not data or "stations" not in data:
            return
        stations = data.get("stations", [])
        if not stations:
            return

        fuel_map = {
            "ai92": "AI-92",
            "ai95": "AI-95",
            "ai98": "AI-98",
            "ai100": "AI-100",
            "dt": "DT",
        }

        async with AsyncSessionLocal() as db:
            try:
                for st in stations:
                    station_id = st.get("id")
                    if not station_id:
                        continue
                    queue_level = st.get("queue_level", "unknown")
                    fuels = st.get("fuels", {})
                    for f_code, f_val in fuels.items():
                        fuel_type = fuel_map.get(str(f_code).lower())
                        if not fuel_type:
                            continue
                        price = f_val.get("price")
                        is_avail = f_val.get("available", True)
                        availability = "available" if is_avail else "unavailable"

                        await db.execute(text("""
                            INSERT INTO station_current_fuel
                                (station_id, fuel_type, price, availability, queue_level, observed_at, source, confidence)
                            VALUES
                                (:station_id, :fuel_type, :price, :availability, :queue_level, NOW(), 'gdebenz', 0.85)
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
                            "queue_level": queue_level
                        })
                await db.commit()
            except Exception as e:
                await db.rollback()
                logger.error(f"[DataCollector] Ошибка транзакции БД: {e}")


async def data_collector_worker():
    """Фоновый воркер сбора данных о наличии топлива и очередях (раз в 15 мин)."""
    logger.info("[DataCollector] Сервис запущен.")
    await asyncio.sleep(15)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                res = await db.execute(text("""
                    SELECT id, name, latitude, longitude 
                    FROM cities 
                    WHERE is_active = true 
                      AND latitude IS NOT NULL 
                      AND longitude IS NOT NULL
                """))
                cities = res.mappings().all()

            for city in cities:
                # --- ЗАЩИТА ОТ None ---
                lat = city.get("latitude")
                lon = city.get("longitude")
                if lat is None or lon is None:
                    logger.warning(f"[DataCollector] Город {city.get('name')} имеет координаты None, пропускаем.")
                    continue
                try:
                    lat = float(lat)
                    lon = float(lon)
                except (TypeError, ValueError) as e:
                    logger.warning(f"[DataCollector] Ошибка преобразования координат города {city.get('name')}: {e}")
                    continue

                # Проверка, что координаты не нулевые (часто означают не заданные)
                if abs(lat) < 0.0001 and abs(lon) < 0.0001:
                    logger.warning(f"[DataCollector] Город {city.get('name')} имеет нулевые координаты, пропускаем.")
                    continue

                data = await DataCollectorService.fetch_city_data(lat - 0.35, lat + 0.35, lon - 0.35, lon + 0.35)
                if data:
                    await DataCollectorService.save_to_db(data)
                await asyncio.sleep(2)  # Плавная нагрузка
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[DataCollector] Ошибка итерации: {e}")
        await asyncio.sleep(900)  # 15 минут
