# utils/geocoder.py
import aiohttp
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)
USER_AGENT = "BinzoLifeBot/2.0 (fuel_station_locator)"

async def reverse_geocode(lat: float, lon: float, session = None) -> Optional[str]:
    """
    Определяет точный человекочитаемый адрес по координатам (lat, lon).
    Сначала проверяет локальный кэш БД, затем делает запрос к Nominatim.
    """
    if lat is None or lon is None or (lat == 0.0 and lon == 0.0):
        return None

    # Локальный импорт для избежания циклической зависимости
    from database.crud import get_cached_address, cache_address

    # 1. Проверяем кэш в БД, если передана сессия
    if session:
        try:
            cached = await get_cached_address(session, lat, lon)
            if cached:
                return cached
        except Exception:
            pass

    # 2. Обратный геокодинг через OpenStreetMap Nominatim
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "lat": lat,
        "lon": lon,
        "format": "jsonv2",
        "addressdetails": 1,
        "accept-language": "ru"
    }
    headers = {
        "User-Agent": USER_AGENT
    }

    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as http_client:
            async with http_client.get(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    addr = data.get("address", {})
                    # Собираем красивый компактный адрес
                    road = addr.get("road") or addr.get("street") or addr.get("pedestrian") or ""
                    house = addr.get("house_number") or ""
                    city = addr.get("city") or addr.get("town") or addr.get("village") or addr.get("county") or ""
                    parts = []
                    if city:
                        parts.append(city)
                    if road:
                        parts.append(road)
                    if house:
                        parts.append(str(house))
                    full_address = ", ".join(parts) if parts else data.get("display_name")
                    if full_address:
                        clean_addr = full_address.strip()
                        # Сохраняем в кэш
                        if session:
                            try:
                                await cache_address(session, lat, lon, clean_addr)
                            except Exception:
                                pass
                        return clean_addr
                elif resp.status == 429:
                    logger.warning("Nominatim rate-limit (429), делаем паузу...")
                    await asyncio.sleep(2)
    except Exception as e:
        logger.debug(f"Ошибка обратного геокодирования ({lat}, {lon}): {e}")
    return None
