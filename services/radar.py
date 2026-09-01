import asyncio
import logging
from datetime import datetime
from aiogram import Bot
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

async def get_city_top3_stations(city_id: int):
    """Получает топ-3 самые дешёвые АЗС в городе (АИ-95) одним запросом."""
    async with AsyncSessionLocal() as db:
        res = await db.execute(text("""
            SELECT s.brand, s.address, f.price
            FROM stations s
            JOIN station_current_fuel f ON s.id = f.station_id
            WHERE s.city_id = :city_id AND f.fuel_type = 'AI-95' AND f.availability = 'available'
            ORDER BY f.price ASC
            LIMIT 3
        """), {"city_id": city_id})
        return res.mappings().all()

async def send_friday_radar(bot: Bot):
    """Рассылает пятничный радар цен всем пользователям."""
    logger.info("[Radar] Запуск формирования пятничного радара...")
    
    try:
        async with AsyncSessionLocal() as db:
            # Получаем всех пользователей с указанным городом
            res = await db.execute(text("""
                SELECT telegram_id, city_id, is_pro 
                FROM users 
                WHERE city_id IS NOT NULL AND telegram_id IS NOT NULL
            """))
            users = res.mappings().all()

        if not users:
            logger.info("[Radar] Нет пользователей для рассылки")
            return

        # Кешируем топы по городам
        city_tops = {}

        for u in users:
            city_id = u["city_id"]
            if city_id not in city_tops:
                city_tops[city_id] = await get_city_top3_stations(city_id)
            
            top = city_tops[city_id]
            if not top:
                continue

            text_lines = ["🚗 <b>Пятничный радар цен BinzoLife</b>\nЛучшие цены на АИ-95 перед выходными:\n"]
            for idx, item in enumerate(top, 1):
                text_lines.append(f"{idx}. <b>{item['brand']}</b> ({item['address']}) — <b>{item['price']:.2f} ₽</b>")
            
            text_lines.append("\n💡 <i>Сэкономьте до 250 ₽ на полном баке!</i>")
            msg_text = "\n".join(text_lines)

            try:
                await bot.send_message(u["telegram_id"], msg_text, parse_mode="HTML")
                await asyncio.sleep(0.05)  # защита от лимитов Telegram
            except Exception as send_err:
                logger.debug(f"[Radar] Не удалось отправить пользователю {u['telegram_id']}: {send_err}")

        logger.info(f"[Radar] Рассылка завершена для {len(users)} пользователей.")
    except Exception as e:
        logger.error(f"[Radar] Критическая ошибка в рассылке радара: {e}")

async def friday_radar_worker(bot: Bot):
    """Фоновый планировщик: проверяет наступление пятницы 17:00."""
    logger.info("[Radar] Планировщик радара запущен")
    while True:
        try:
            now = datetime.now()
            # Пятница (weekday == 4), время 17:00–17:05
            if now.weekday() == 4 and now.hour == 17 and now.minute < 10:
                await send_friday_radar(bot)
                # Ждём 1 час, чтобы не отправить повторно в этот же день
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Radar] Ошибка планировщика: {e}")
        await asyncio.sleep(60)
