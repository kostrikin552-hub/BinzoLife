# services/radar.py — ПОЛНАЯ ИСПРАВЛЕННАЯ ВЕРСИЯ
import asyncio
import logging
from datetime import datetime
from aiogram import Bot
from sqlalchemy import text
from database.session import AsyncSessionLocal
from services.notifications import safe_broadcast

logger = logging.getLogger(__name__)

async def process_user_batch(bot: Bot, users: list):
    """Обрабатывает пачку пользователей (до 100) для рассылки радара."""
    if not users:
        return
    # Группируем по городам
    city_groups = {}
    for u in users:
        city_id = u["city_id"]
        if city_id not in city_groups:
            city_groups[city_id] = []
        city_groups[city_id].append(u)

    # Для каждого города получаем топ-3 и отправляем
    for city_id, city_users in city_groups.items():
        async with AsyncSessionLocal() as db:
            top = (await db.execute(text("""
                SELECT s.brand, s.address, f.price
                FROM stations s
                JOIN fuel_prices f ON s.id = f.station_id
                WHERE s.city_id = :cid AND f.fuel_type = 'AI-95' AND f.is_fresh = true
                ORDER BY f.price ASC
                LIMIT 3;
            """), {"cid": city_id})).mappings().all()
        if not top:
            continue
        lines = ["🚗 <b>Пятничный радар цен BinzoLife</b>\nВыгодный АИ-95 на выходные:\n"]
        for idx, item in enumerate(top, 1):
            lines.append(f"{idx}. <b>{item['brand']}</b> ({item['address']}) — <b>{item['price']:.2f} ₽</b>")
        lines.append("\n💡 <i>Экономьте на каждой заправке с BinzoLife!</i>")
        msg_text = "\n".join(lines)
        user_ids = [u["telegram_id"] for u in city_users]
        await safe_broadcast(bot, user_ids, msg_text)

async def broadcast_friday_radar(bot: Bot):
    logger.info("[FridayRadar] Старт рассылки лучших цен...")
    try:
        async with AsyncSessionLocal() as db:
            page_size = 100
            offset = 0
            while True:
                res = await db.execute(text("""
                    SELECT telegram_id, city_id
                    FROM users
                    WHERE is_active = true AND city_id IS NOT NULL
                    ORDER BY id
                    LIMIT :limit OFFSET :offset
                """), {"limit": page_size, "offset": offset})
                users = res.mappings().all()
                if not users:
                    break
                offset += page_size
                await process_user_batch(bot, users)
                await asyncio.sleep(1)  # дать памяти освободиться
        logger.info("[FridayRadar] Рассылка завершена.")
    except Exception as e:
        logger.error(f"[FridayRadar] Ошибка рассылки: {e}")

async def friday_radar_worker(bot: Bot):
    logger.info("[FridayRadar] Планировщик запущен.")
    while True:
        try:
            now = datetime.now()
            if now.weekday() == 4 and now.hour == 17 and now.minute < 5:
                await broadcast_friday_radar(bot)
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[FridayRadar] Ошибка планировщика: {e}")
        await asyncio.sleep(60)
