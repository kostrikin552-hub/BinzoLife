import aiohttp
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

YANDEX_GEOCODER_API_KEY = "d120fde6-3e4d-4c93-9e89-1d0eb2113536"
YANDEX_GEOCODER_URL = "https://geocode-maps.yandex.ru/1.x/?apikey={}&geocode={}&format=json"

async def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Преобразует адрес в координаты (широта, долгота) через Yandex Geocoder.
    Возвращает (lat, lon) или None.
    """
    if not YANDEX_GEOCODER_API_KEY:
        logger.warning("Yandex Geocoder API key не настроен")
        return None

    url = YANDEX_GEOCODER_URL.format(YANDEX_GEOCODER_API_KEY, address.replace(" ", "+"))
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
