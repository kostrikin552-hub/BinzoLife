# services/address_updater.py
import asyncio
import logging
from database.session import AsyncSessionLocal
from database.crud import fill_missing_station_addresses

logger = logging.getLogger(__name__)

async def address_updater_worker():
    """Фоновый воркер: каждые 30 минут подтягивает адреса для новых АЗС"""
    logger.info("[AddressUpdater] Фоновый сервис запущен.")
    # Первый запуск через 2 минуты после старта контейнера
    await asyncio.sleep(120)
    while True:
        try:
            async with AsyncSessionLocal() as session:
                updated, found = await fill_missing_station_addresses(session, limit=30)
                if updated > 0:
                    logger.info(f"[AddressUpdater] Автоматически обновлено {updated} из {found} адресов АЗС.")
            # Спим 30 минут до следующей проверки
            await asyncio.sleep(1800)
        except asyncio.CancelledError:
            logger.info("[AddressUpdater] Воркер остановлен.")
            break
        except Exception as e:
            logger.error(f"[AddressUpdater] Ошибка в цикле: {e}")
            await asyncio.sleep(300)

# Алиас для совместимости с main.py
run_address_updater = address_updater_worker
