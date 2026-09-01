# services/address_updater.py
import asyncio
import logging
from aiogram import Bot
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

async def update_missing_addresses():
    """Обновляет недостающие адреса и координаты АЗС."""
    try:
        async with AsyncSessionLocal() as db:
            stmt = text("""
                SELECT id, name, latitude, longitude, address 
                FROM stations 
                WHERE (address IS NULL OR address = '') 
                  AND latitude IS NOT NULL 
                  AND longitude IS NOT NULL
                LIMIT 50;
            """)
            stations = (await db.execute(stmt)).mappings().all()

            if not stations:
                return

            for st in stations:
                # В случае отсутствия адреса подставляем читаемые координаты/имя
                clean_addr = f"Координаты: {st['latitude']:.4f}, {st['longitude']:.4f}"
                await db.execute(text("""
                    UPDATE stations 
                    SET address = :addr 
                    WHERE id = :id;
                """), {"addr": clean_addr, "id": st["id"]})
            
            await db.commit()
            logger.info(f"[AddressUpdater] Обновлено {len(stations)} адресов.")
    except Exception as e:
        logger.error(f"[AddressUpdater] Ошибка обновления адресов: {e}")

async def address_updater_worker(bot: Bot = None):
    """
    Основной фоновый воркер для main.py (запуск раз в 6 часов).
    """
    logger.info("[AddressUpdater] Сервис обновления адресов запущен.")
    await asyncio.sleep(90)
    while True:
        try:
            await update_missing_addresses()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[AddressUpdater] Ошибка воркера: {e}")
        await asyncio.sleep(21600)

# Алиасы для обратной совместимости
run_address_updater = address_updater_worker
