# services/radar.py
import asyncio
import logging
from datetime import datetime
from aiogram import Bot
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

async def broadcast_friday_radar(bot: Bot):
    """Отправка пятничного радара с лучшими ценами."""
    logger.info("[FridayRadar] Старт рассылки лучших цен...")
    try:
        async with AsyncSessionLocal() as db:
            users = (await db.execute(text("""
                SELECT telegram_id, city_id 
                FROM users 
                WHERE is_active = true AND city_id IS NOT NULL;
            """))).mappings().all()

        if not users:
            return

        city_cache = {}
        for u in users:
            cid = u["city_id"]
            if cid not in city_cache:
                async with AsyncSessionLocal() as db:
                    top = (await db.execute(text("""
                        SELECT s.brand, s.address, f.price
                        FROM stations s
                        JOIN fuel_prices f ON s.id = f.station_id
                        WHERE s.city_id = :cid AND f.fuel_type = 'AI-95' AND f.is_fresh = true
                        ORDER BY f.price ASC
                        LIMIT 3;
                    """), {"cid": cid})).mappings().all()
                    city_cache[cid] = top

            best_stations = city_cache.get(cid)
            if not best_stations:
                continue

            lines = ["🚗 <b>Пятничный радар цен BinzoLife</b>\nВыгодный АИ-95 на выходные:\n"]
            for idx, item in enumerate(best_stations, 1):
                lines.append(f"{idx}. <b>{item['brand']}</b> ({item['address']}) — <b>{item['price']:.2f} ₽</b>")
            lines.append("\n💡 <i>Экономьте на каждой заправке с BinzoLife!</i>")

            try:
                await bot.send_message(u["telegram_id"], "\n".join(lines), parse_mode="HTML")
                await asyncio.sleep(0.05)  # Защита от лимитов Telegram
            except Exception:
                pass

        logger.info(f"[FridayRadar] Рассылка завершена для {len(users)} пользователей.")
    except Exception as e:
        logger.error(f"[FridayRadar] Ошибка рассылки: {e}")

async def friday_radar_worker(bot: Bot):
    """Планировщик пятничной рассылки."""
    logger.info("[FridayRadar] Планировщик запущен.")
    while True:
        try:
            now = datetime.now()
            # Пятница (weekday == 4) в 17:00
            if now.weekday() == 4 and now.hour == 17 and now.minute < 5:
                await broadcast_friday_radar(bot)
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[FridayRadar] Ошибка планировщика: {e}")
        await asyncio.sleep(60)
