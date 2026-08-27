import aiohttp
import logging
import time
import asyncio
from typing import Optional, Tuple
from config import settings

logger = logging.getLogger(__name__)

YANDEX_GEOCODER_URL = "https://geocode-maps.yandex.ru/1.x/?apikey={}&geocode={}&format=json"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse?lat={}&lon={}&format=json&zoom=18&addressdetails=1"
NOMINATIM_HEADERS = {"User-Agent": "BinzoLifeBot/1.0"}

# Ограничение частоты
_last_reverse_request_time = 0
_REVERSE_LOCK = asyncio.Lock()

async def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """Прямое геокодирование: адрес → координаты (только Яндекс)"""
    api_key = settings.YANDEX_GEOCODER_API_KEY
    if not api_key:
        logger.warning("Yandex Geocoder API key не настроен")
        return None

    url = YANDEX_GEOCODER_URL.format(api_key, address.replace(" ", "+"))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"Геокодер вернул статус {resp.status}")
                    return None
                data = await resp.json()
                geo_objects = data.get("response", {}).get("GeoObjectCollection", {}).get("featureMember", [])
                if not geo_objects:
                    return None
                coords_str = geo_objects[0].get("GeoObject", {}).get("Point", {}).get("pos", "")
                if not coords_str:
                    return None
                lon, lat = map(float, coords_str.split())
                return lat, lon
    except Exception as e:
        logger.error(f"Ошибка геокодирования: {e}")
        return None

async def reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """
    Обратное геокодирование: координаты → адрес.
    Сначала Яндекс (если есть ключ), потом Nominatim.
    С ограничением частоты запросов (минимум 0.5 сек между вызовами).
    """
    if lat == 0.0 and lon == 0.0:
        return None

    global _last_reverse_request_time
    async with _REVERSE_LOCK:
        now = time.time()
        if now - _last_reverse_request_time < 0.5:
            await asyncio.sleep(0.5 - (now - _last_reverse_request_time))
        _last_reverse_request_time = time.time()

    # 1. Яндекс (если есть ключ)
    if settings.YANDEX_GEOCODER_API_KEY:
        try:
            yandex_url = f"https://geocode-maps.yandex.ru/1.x/?apikey={settings.YANDEX_GEOCODER_API_KEY}&geocode={lon},{lat}&format=json"
            async with aiohttp.ClientSession() as session:
                async with session.get(yandex_url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        geo_objects = data.get("response", {}).get("GeoObjectCollection", {}).get("featureMember", [])
                        if geo_objects:
                            addr = geo_objects[0].get("GeoObject", {}).get("metaDataProperty", {}).get("GeocoderMetaData", {}).get("text", "")
                            if addr:
                                return addr
        except Exception as e:
            logger.warning(f"Ошибка обратного геокодирования (Яндекс): {e}")

    # 2. Nominatim (бесплатный)
    try:
        url = NOMINATIM_REVERSE_URL.format(lat, lon)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=NOMINATIM_HEADERS, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "display_name" in data:
                        address = data.get("display_name", "")
                        parts = address.split(", ")
                        if len(parts) > 3:
                            short_addr = ", ".join(parts[:3])
                            return short_addr
                        return address
    except Exception as e:
        logger.warning(f"Ошибка обратного геокодирования (Nominatim): {e}")

    return None
