# services/fuelprice_parser.py — УСИЛЕННАЯ ВЕРСИЯ (с защитой от 403)
import asyncio
import re
import logging
import random
from typing import List, Dict
import aiohttp
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from database.models import City, User
from database.crud import update_city_prices_by_brand
from utils.task_locks import task_locker

logger = logging.getLogger(__name__)

BASE_URL = "https://fuelprice.ru"

# Расширенный список User-Agent (современные браузеры)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
]

FUEL_TYPE_MAP = {
    "АИ-92": "AI-92",
    "АИ-95": "AI-95",
    "АИ-98": "AI-98",
    "АИ-100": "AI-100",
    "ДТ": "DT",
}

class HybridFuelParser:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=30, connect=15)
        self.session = None  # будет создана при первом запросе

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Connection": "keep-alive",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                }
            )
        return self.session

    def _get_headers(self) -> Dict[str, str]:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": "https://yandex.ru/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }

    async def fetch_fuelprice_city(self, city_slug: str) -> List[Dict]:
        url = f"{BASE_URL}/{city_slug}"
        results = []
        session = await self._get_session()

        for attempt in range(1, 6):  # увеличили до 5 попыток
            try:
                async with session.get(url, headers=self._get_headers()) as response:
                    if response.status == 404:
                        logger.warning(f"Город '{city_slug}' не найден (404)")
                        return []

                    if response.status == 403:
                        logger.warning(f"fuelprice.ru/{city_slug}: 403 Forbidden (попытка {attempt}), ждём {attempt * 10} сек...")
                        await asyncio.sleep(attempt * 10)
                        # Если 403, меняем User-Agent на следующий запрос
                        continue

                    if response.status != 200:
                        logger.warning(f"fuelprice.ru/{city_slug}: статус {response.status}, попытка {attempt}")
                        await asyncio.sleep(attempt * 5)
                        continue

                    html = await response.text()

                    # 1. Основной способ: JS-массивы
                    js_pattern = re.compile(
                        r'\[([\d.]+),\s*([\d.]+),\s*\'([^\']+)\',\s*\'([^\']*)\',\s*\'([^\']*)\''
                    )
                    matches = js_pattern.findall(html)

                    if matches:
                        for m in matches:
                            brand_name = m[2].strip()
                            fuel_str = m[4].strip() if len(m) > 4 else ""
                            for fuel_key, fuel_val in FUEL_TYPE_MAP.items():
                                p_match = re.search(rf'{re.escape(fuel_key)}\s*[:：]\s*([\d.,]+)', fuel_str, re.IGNORECASE)
                                if p_match:
                                    try:
                                        price = float(p_match.group(1).replace(",", "."))
                                        if 35.0 <= price <= 150.0:
                                            results.append({
                                                "fuel_type": fuel_val,
                                                "price": price,
                                                "brand": brand_name,
                                                "source": "fuelprice.ru"
                                            })
                                    except ValueError:
                                        continue

                    # 2. Fallback: поиск в тексте
                    if not results:
                        for fuel_key, fuel_val in FUEL_TYPE_MAP.items():
                            text_matches = re.findall(
                                rf'{re.escape(fuel_key)}[^0-9\n\r]{{1,20}}(\d{{2}}[.,]\d{{2}})',
                                html,
                                re.IGNORECASE
                            )
                            for p_str in text_matches:
                                try:
                                    price = float(p_str.replace(",", "."))
                                    if 35.0 <= price <= 150.0:
                                        results.append({
                                            "fuel_type": fuel_val,
                                            "price": price,
                                            "brand": None,
                                            "source": "fuelprice.ru"
                                        })
                                except ValueError:
                                    continue

                    if results:
                        logger.info(f"✅ fuelprice.ru: получено {len(results)} цен для '{city_slug}'")
                        return results

            except asyncio.TimeoutError:
                logger.warning(f"Таймаут при запросе {city_slug} (попытка {attempt})")
                await asyncio.sleep(attempt * 5)
            except Exception as e:
                logger.debug(f"Ошибка при запросе {city_slug} (попытка {attempt}): {e}")
                await asyncio.sleep(attempt * 4)

        return results

    async def run_daily_parse_all_cities(self, session_factory):
        if not task_locker.acquire("daily_fuel_parser", timeout_seconds=7200):
            logger.warning("Парсер уже выполняется, пропуск.")
            return

        try:
            logger.info("🚀 Запуск обновления цен по городам РФ с fuelprice.ru...")
            # Случайная задержка перед стартом, чтобы не быть слишком предсказуемым
            await asyncio.sleep(random.uniform(5, 15))

            async with session_factory() as session:
                active_city_ids = set(
                    (await session.execute(
                        select(User.city_id).where(User.city_id.isnot(None)).distinct()
                    )).scalars().all()
                )
                all_cities = (await session.execute(
                    select(City).options(joinedload(City.city_slug)).where(City.is_active.is_(True))
                )).scalars().all()

                sorted_cities = sorted(all_cities, key=lambda c: 0 if c.id in active_city_ids else 1)

            total_updated = 0
            for idx, city in enumerate(sorted_cities, start=1):
                slug = city.slug or (city.city_slug.slug if city.city_slug else None)
                if not slug:
                    continue

                logger.info(f"[{idx}/{len(sorted_cities)}] Сбор цен для г. {city.name} (slug: {slug})...")
                prices = await self.fetch_fuelprice_city(slug)

                if prices:
                    async with session_factory() as session:
                        updated_count = 0
                        for item in prices:
                            count = await update_city_prices_by_brand(
                                session=session,
                                city_id=city.id,
                                fuel_type=item["fuel_type"],
                                price=item["price"],
                                brand_pattern=item.get("brand"),
                                source=item["source"]
                            )
                            updated_count += count
                        await session.commit()
                        total_updated += updated_count
                        logger.info(f"Обновлено {updated_count} станций в г. {city.name}")
                else:
                    logger.warning(f"Не удалось получить цены для {city.name}")

                # Увеличенная пауза между городами (5-8 секунд)
                await asyncio.sleep(random.uniform(5.0, 8.0))

            logger.info(f"🎉 Сбор цен завершён! Обновлено котировок: {total_updated}")

        finally:
            task_locker.release("daily_fuel_parser")
            # Закрываем сессию, чтобы освободить ресурсы
            if self.session and not self.session.closed:
                await self.session.close()
                self.session = None

fuel_parser = HybridFuelParser()

async def fuel_price_parser_worker():
    from database.session import AsyncSessionLocal
    logger.info("[FuelPriceParser] Фоновый воркер запущен.")
    await asyncio.sleep(30)
    while True:
        try:
            await fuel_parser.run_daily_parse_all_cities(AsyncSessionLocal)
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[FuelPriceParser] Ошибка: {e}", exc_info=True)
            await asyncio.sleep(300)
