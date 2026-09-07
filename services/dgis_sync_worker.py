# services/dgis_sync_worker.py — воркер для MultiGo V2
import asyncio
import logging
from services.multigo_v2 import multigo_v2

logger = logging.getLogger(__name__)


async def fuel_price_parser_worker():
    from database.session import AsyncSessionLocal
    logger.info("[MultiGo] Воркер запущен.")
    await asyncio.sleep(45)
    while True:
        try:
            await multigo_v2.sync_all_cities(AsyncSessionLocal)
            await asyncio.sleep(24 * 3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[MultiGo] Ошибка воркера: {e}", exc_info=True)
            await asyncio.sleep(300)
