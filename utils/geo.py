import logging
import aiohttp
import json
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Кэш: {ip: (city_name, timestamp)}
_cache = {}
CACHE_TTL = timedelta(hours=24)
IP_API_URL = "http://ip-api.com/json/{}?fields=status,message,city,country"

async def get_city_by_ip(ip: str) -> Optional[str]:
    """
    Определяет город по IP через ip-api.com.
    Возвращает название города или None.
    """
    # Проверяем кэш
    if ip in _cache:
        city, timestamp = _cache[ip]
        if datetime.now() - timestamp < CACHE_TTL:
            logger.info(f"Город {city} взят из кэша для IP {ip}")
            return city
        else:
            del _cache[ip]

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(IP_API_URL.format(ip), timeout=5) as resp:
                if resp.status != 200:
                    logger.error(f"IP-API вернул статус {resp.status}")
                    return None
                data = await resp.json()
                if data.get("status") != "success":
                    logger.error(f"IP-API вернул ошибку: {data.get('message')}")
                    return None
                city = data.get("city")
                if not city:
                    return None
                # Сохраняем в кэш
                _cache[ip] = (city, datetime.now())
                logger.info(f"Город {city} определён для IP {ip}")
                return city
    except aiohttp.ClientError as e:
        logger.error(f"Ошибка при запросе к IP-API: {e}")
        return None
    except Exception as e:
        logger.error(f"Неизвестная ошибка при определении города: {e}")
        return None

def get_cached_city(ip: str) -> Optional[str]:
    """Возвращает город из кэша, если он ещё актуален."""
    if ip in _cache:
        city, timestamp = _cache[ip]
        if datetime.now() - timestamp < CACHE_TTL:
            return city
    return None

def set_cached_city(ip: str, city: str) -> None:
    """Принудительно сохраняет город в кэш."""
    _cache[ip] = (city, datetime.now())
