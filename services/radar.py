# services/radar.py — ПОЛНАЯ ВЕРСИЯ (все изменения из этапов 1–5)
import asyncio
import logging
import html
from datetime import datetime
from typing import List, Dict, Any
from aiogram import Bot
from sqlalchemy import text
from database.session import AsyncSessionLocal
from services.notifications import safe_broadcast

logger = logging.getLogger(__name__)


async def process_user_batch(bot: Bot, users: List[Dict[str, Any]]):
    """Обрабатывает пачку пользователей (до 100) для рассылки радара."""
    if not users:
        return

    # Группируем по городам
    city_groups = {}
    for u in users:
        city_id = u.get("city_id")
        if not city_id:
            continue
        # Проверяем наличие координат (для будущей персонализации)
        if u.get("last_lat") is None or u.get("last_lon") is None:
            # Если координат нет — можно пропустить или отправить общую сводку
            # Пока оставляем, так как радар показывает топ-3 по городу, а не по расстоянию
            pass
        if city_id not in city_groups:
            city_groups[city_id] = []
        city_groups[city_id].append(u)

    for city_id, city_users in city_groups.items():
        async with AsyncSessionLocal() as db:
            # Получаем топ-3 дешёвые станции в городе (АИ-95)
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

        # Персонализированный текст радара
        first_user = city_users[0]
        avg_price = sum(s["price"] for s in top) / len(top)
        best = top[0]
        diff_per_liter = avg_price - best["price"]
        tank_volume = 50.0  # можно подгрузить из профиля, но для простоты используем 50 л
        tank_savings = diff_per_liter * tank_volume if diff_per_liter > 0 else 0

        lines = [
            "☀️ <b>Доброе утро пятницы! Время заправить бак на выходные</b> 🚗💨\n",
            "Мы просканировали заправки вашего района на свежесть цен:\n",
            f"\n🏆 <b>Лидер экономии сегодня:</b>\n",
            f"⛽ <b>{html.escape(best['brand'] or 'АЗС')}</b> — <b>{best['price']:.2f} ₽</b> (в среднем по городу {avg_price:.2f} ₽)\n",
            f"📍 <code>{html.escape(best['address'] or 'адрес уточняется')}</code>\n",
        ]
        if tank_savings > 0:
            lines.append(f"💵 Заправив бак здесь, вы оставите в кошельке <b>+{tank_savings:.0f} ₽</b> чистыми!\n")
        else:
            lines.append("💡 Цены практически равны средним по городу.\n")

        lines.append("\n<i>Удачных поездок и ровных дорог в эти выходные!</i> 🛣")

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
                    SELECT telegram_id, city_id, last_lat, last_lon
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
            # Пятница (weekday == 4) в 17:00
            if now.weekday() == 4 and now.hour == 17 and now.minute < 5:
                await broadcast_friday_radar(bot)
                await asyncio.sleep(3600)  # не повторять в этот день
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[FridayRadar] Ошибка планировщика: {e}")
        await asyncio.sleep(60)
