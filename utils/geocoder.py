import aiohttp
import logging
from typing import Optional, Tuple
from config import settings  # добавлен импорт

logger = logging.getLogger(__name__)

# Ключ больше не хардкодится, берётся из настроек
YANDEX_GEOCODER_URL = "https://geocode-maps.yandex.ru/1.x/?apikey={}&geocode={}&format=json"

async def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    api_key = settings.YANDEX_GEOCODER_API_KEY
    if not api_key:
        logger.warning("Yandex Geocoder API key не настроен (переменная YANDEX_GEOCODER_API_KEY)")
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
