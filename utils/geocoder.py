import aiohttp
import logging
from typing import Optional, Tuple
from config import settings

logger = logging.getLogger(__name__)

YANDEX_GEOCODER_URL = "https://geocode-maps.yandex.ru/1.x/?apikey={}&geocode={}&format=json"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse?lat={}&lon={}&format=json&zoom=18&addressdetails=1"
NOMINATIM_HEADERS = {"User-Agent": "BinzoLifeBot/1.0"}

async def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Прямое геокодирование: адрес → координаты.
    Использует Яндекс.Геокодер (требуется API-ключ).
    """
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
    Сначала пытается через Nominatim (бесплатно, без ключа).
    Если не получается, пробует Яндекс.Геокодер (если ключ задан).
    """
    if lat == 0.0 and lon == 0.0:
        return None

    # 1. Пробуем Nominatim (бесплатно, но с ограничением 1 запрос/сек)
    try:
        url = NOMINATIM_REVERSE_URL.format(lat, lon)
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=NOMINATIM_HEADERS, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "display_name" in data:
                        address = data.get("display_name", "")
                        # Сокращаем адрес (убираем страну и лишние детали)
                        parts = address.split(", ")
                        if len(parts) > 3:
                            # Берём первые 3 части (улица, район, город) – обычно этого достаточно
                            short_addr = ", ".join(parts[:3])
                            return short_addr
                        return address
    except Exception as e:
        logger.warning(f"Ошибка обратного геокодирования (Nominatim): {e}")

    # 2. Если Nominatim не сработал и есть Яндекс-ключ – пробуем Яндекс
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

    return None
