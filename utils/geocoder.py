import aiohttp
import logging
from typing import Optional, Tuple
from config import settings

logger = logging.getLogger(__name__)

# Яндекс (требуется ключ)
YANDEX_URL = "https://geocode-maps.yandex.ru/1.x/?apikey={}&geocode={}&format=json"

# Nominatim (бесплатный, без ключа)
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search?q={}&format=json&limit=1"
NOMINATIM_HEADERS = {"User-Agent": "BinzoLifeBot/1.0"}


async def geocode_address(address: str) -> Optional[Tuple[float, float]]:
    """
    Геокодирование адреса.
    Сначала пытается через Яндекс.Геокодер (если есть ключ).
    При ошибке 403 или отсутствии ключа — использует Nominatim.
    """
    # 1. Попытка через Яндекс
    api_key = settings.YANDEX_GEOCODER_API_KEY
    if api_key:
        try:
            coords = await _geocode_yandex(address, api_key)
            if coords:
                return coords
            logger.warning("Яндекс не вернул координаты, пробуем Nominatim")
        except Exception as e:
            logger.error(f"Яндекс геокодер упал: {e}, пробуем Nominatim")
    else:
        logger.info("Ключ Яндекса не задан, используем Nominatim")

    # 2. Резерв — Nominatim
    return await _geocode_nominatim(address)


async def _geocode_yandex(address: str, api_key: str) -> Optional[Tuple[float, float]]:
    url = YANDEX_URL.format(api_key, address.replace(" ", "+"))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"Яндекс вернул статус {resp.status}")
                    # Если 403 — ключ невалидный, переходим к Nominatim
                    if resp.status == 403:
                        logger.warning("Ключ Яндекса не авторизован (403), используем Nominatim")
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
        logger.error(f"Ошибка запроса к Яндекс: {e}")
        return None


async def _geocode_nominatim(address: str) -> Optional[Tuple[float, float]]:
    url = NOMINATIM_URL.format(address.replace(" ", "+"))
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=NOMINATIM_HEADERS, timeout=10) as resp:
                if resp.status != 200:
                    logger.error(f"Nominatim вернул статус {resp.status}")
                    return None
                data = await resp.json()
                if not data:
                    return None
                lat = float(data[0]["lat"])
                lon = float(data[0]["lon"])
                return lat, lon
    except Exception as e:
        logger.error(f"Ошибка запроса к Nominatim: {e}")
        return None
