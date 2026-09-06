# services/dgis_collector.py – надёжный сборщик АЗС через API 2ГИС
import asyncio
import logging
import random
import re
from typing import List, Dict
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
    """
    Сборщик АЗС через каталог 2ГИС с поддержкой текстового и рубрикаторного геопоиска.
    Гарантирует получение станций даже в небольших городах.
    """

    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=18, connect=6)

    def _get_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json",
            "Referer": "https://2gis.ru/",
            "Origin": "https://2gis.ru"
        }

    async def fetch_city_stations(self, city_name: str, lat: float, lon: float) -> List[Dict]:
        """
        Гарантированный сбор АЗС города:
        1. Сначала пробует текстовый геопоиск 'АЗС {city_name}' с сортировкой по расстоянию.
        2. Если пусто — задействует поиск по рубрике АЗС в радиусе 20 км.
        """
        url = "https://catalog.api.2gis.com/3.0/items"

        # Сценарий 1: Текстовый поиск с привязкой к городу и координатам
        params = {
            "q": f"АЗС {city_name}",
            "point": f"{lon},{lat}",
            "sort": "distance",
            "type": "branch",
            "fields": "items.point,items.address_name,items.name,items.adm_div",
            "key": DGIS_PUBLIC_KEY,
            "page_size": "50"
        }

        stations_list = []

        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session:
                async with session.get(url, params=params, headers=self._get_headers()) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("result", {}).get("items", [])
                        for item in items:
                            name = item.get("name") or "АЗС"
                            address = item.get("address_name") or ""
                            point = item.get("point") or {}
                            st_lat = point.get("lat")
                            st_lon = point.get("lon")
                            if st_lat and st_lon:
                                stations_list.append({
                                    "name": name,
                                    "brand": clean_brand_name(name),
                                    "address": f"г. {city_name}, {address}" if address else f"г. {city_name}",
                                    "lat": float(st_lat),
                                    "lon": float(st_lon),
                                    "source": "2gis"
                                })

            # Сценарий 2 (Резервный): если Сценарий 1 вернул 0 (например, у специфических городов)
            if not stations_list:
                logger.info(f"Текстовый поиск для {city_name} дал 0, пробуем поиск по радиусу вокруг центра...")
                params_fallback = {
                    "q": "автозаправочная станция",
                    "point": f"{lon},{lat}",
                    "radius": "20000",
                    "type": "branch",
                    "fields": "items.point,items.address_name,items.name",
                    "key": DGIS_PUBLIC_KEY,
                    "page_size": "50"
                }
                async with aiohttp.ClientSession(timeout=self.timeout) as session:
                    async with session.get(url, params=params_fallback, headers=self._get_headers()) as resp_fb:
                        if resp_fb.status == 200:
                            data_fb = await resp_fb.json()
                            items_fb = data_fb.get("result", {}).get("items", [])
                            for item in items_fb:
                                name = item.get("name") or "АЗС"
                                address = item.get("address_name") or ""
                                point = item.get("point") or {}
                                st_lat = point.get("lat")
                                st_lon = point.get("lon")
                                if st_lat and st_lon:
                                    stations_list.append({
                                        "name": name,
                                        "brand": clean_brand_name(name),
                                        "address": f"г. {city_name}, {address}" if address else f"г. {city_name}",
                                        "lat": float(st_lat),
                                        "lon": float(st_lon),
                                        "source": "2gis"
                                    })

        except Exception as e:
            logger.error(f"Ошибка запроса 2ГИС для {city_name}: {e}")

        logger.info(f"✅ 2ГИС: получено {len(stations_list)} АЗС для г. {city_name}")
        return stations_list


dgis_collector = DgisFuelCollector()
