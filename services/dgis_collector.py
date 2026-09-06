# services/dgis_collector.py
import asyncio
import logging
import random
import re
from typing import List, Dict, Optional
import aiohttp

logger = logging.getLogger(__name__)

# Публичный ключ веб-версии 2ГИС (используется на 2gis.ru)
DGIS_PUBLIC_KEY = "rurbbn3446"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"
]

KNOWN_BRANDS = [
    ("Лукойл", ["лукойл", "lukoil"]),
    ("Газпромнефть", ["газпромнефть", "газпром нефть", "g-drive", "опти"]),
    ("Татнефть", ["татнефть", "tatneft"]),
    ("Роснефть", ["роснефть", "rosneft", "тнк", "башнефть"]),
    ("Teboil", ["teboil", "тебойл", "shell", "шелл"]),
    ("Ирбис", ["irbis", "ирбис"]),
    ("Нефтьмагистраль", ["нефтьмагистраль"]),
    ("Сургутнефтегаз", ["сургутнефтегаз"]),
    ("Транснефть", ["транснефть"]),
]

def clean_brand_name(raw_name: str) -> str:
    """Очищает юридический шум и выделяет чистый маркетинговый бренд"""
    if not raw_name:
        return "АЗС"
    low = raw_name.lower()
    for clean_brand, patterns in KNOWN_BRANDS:
        for p in patterns:
            if p in low:
                return clean_brand
    cleaned = re.sub(r'(?i)(ооо|зао|пао|азс|№|\bазгс\b|сеть|станция)\s*', '', raw_name)
    cleaned = cleaned.replace('"', '').replace("'", "").strip()
    return cleaned.capitalize() if len(cleaned) >= 3 else "АЗС"


class DgisFuelCollector:
    """Надёжный сборщик станций и координат через публичный шлюз каталога 2ГИС"""

    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=20, connect=8)

    def _get_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json",
            "Referer": "https://2gis.ru/",
            "Origin": "https://2gis.ru"
        }

    async def fetch_city_stations(self, city_name: str, lat: float, lon: float, radius_m: int = 25000) -> List[Dict]:
        """
        Запрашивает АЗС города в радиусе radius_m вокруг центра
        Возвращает список словарей с полями: name, brand, address, lat, lon, source
        """
        url = "https://catalog.api.2gis.com/3.0/items"
        params = {
            "q": "АЗС",
            "point": f"{lon},{lat}",
            "radius": str(radius_m),
            "type": "branch",
            "fields": "items.point,items.address_name,items.adm_div,items.rubrics",
            "key": DGIS_PUBLIC_KEY,
            "page_size": "50"
        }
        stations_list = []

        for attempt in range(1, 4):
            try:
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.get(url, params=params, headers=self._get_headers()) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            items = data.get("result", {}).get("items", [])
                            for item in items:
                                raw_name = item.get("name") or "АЗС"
                                address = item.get("address_name") or ""
                                point = item.get("point") or {}
                                st_lat = point.get("lat")
                                st_lon = point.get("lon")
                                if not (st_lat and st_lon):
                                    continue
                                clean_brand = clean_brand_name(raw_name)
                                stations_list.append({
                                    "name": raw_name,
                                    "brand": clean_brand,
                                    "address": address or f"г. {city_name}",
                                    "lat": float(st_lat),
                                    "lon": float(st_lon),
                                    "source": "2gis"
                                })
                            logger.info(f"✅ 2ГИС: получено {len(stations_list)} АЗС для г. {city_name}")
                            return stations_list
                        elif resp.status in (429, 502, 503):
                            logger.warning(f"2ГИС статус {resp.status} для {city_name}, пауза {attempt*2} сек")
                            await asyncio.sleep(attempt * 2)
                        else:
                            logger.warning(f"2ГИС статус {resp.status} для {city_name}")
                            await asyncio.sleep(1.5)
            except Exception as e:
                logger.debug(f"Попытка {attempt} 2ГИС для {city_name} завершилась ошибкой: {e}")
                await asyncio.sleep(1.5)

        return stations_list


dgis_collector = DgisFuelCollector()
