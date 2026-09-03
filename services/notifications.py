# services/notifications.py — ПОЛНАЯ ИСПРАВЛЕННАЯ ВЕРСИЯ
import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Union
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

async def safe_broadcast(bot: Bot, user_ids: List[int], text: str, parse_mode: str = "HTML") -> dict:
    """
    Безопасная рассылка с защитой от 429 Too Many Requests.
    Возвращает { "success": int, "blocked": int }
    """
    success = 0
    blocked = 0
    for i, uid in enumerate(user_ids):
        try:
            await bot.send_message(uid, text, parse_mode=parse_mode)
            success += 1
        except TelegramForbiddenError:
            blocked += 1
            # Помечаем пользователя как неактивного
            async with AsyncSessionLocal() as db:
                await db.execute(text("UPDATE users SET is_active = false WHERE telegram_id = :uid"), {"uid": uid})
                await db.commit()
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
            await bot.send_message(uid, text, parse_mode=parse_mode)
            success += 1
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {uid}: {e}")
        # Пауза после каждых 25 сообщений (≈25 msg/сек)
        if (i + 1) % 25 == 0:
            await asyncio.sleep(1.05)
    return {"success": success, "blocked": blocked}

async def send_user_notification(
    bot: Bot,
    telegram_id: int,
    message: str,
    keyboard: Optional[Any] = None,
    parse_mode: str = "HTML"
) -> bool:
    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=message,
            reply_markup=keyboard,
            parse_mode=parse_mode,
            disable_web_page_preview=True
        )
        return True
    except Exception as e:
        logger.debug(f"[Notifications] Не удалось отправить сообщение {telegram_id}: {e}")
        return False

class NotificationService:
    @staticmethod
    async def notify_price_drop(bot: Bot, user_id: int, station_name: str, fuel_type: str, old_price: float, new_price: float):
        diff = old_price - new_price
        msg = (
            f"📉 <b>Цена снизилась!</b>\n\n"
            f"⛽ АЗС: <b>{station_name}</b>\n"
            f"⛽ Топливо: <b>{fuel_type}</b>\n"
            f"💰 Старая цена: {old_price:.2f} ₽\n"
            f"🔥 Новая цена: <b>{new_price:.2f} ₽</b> (выгода {diff:.2f} ₽/л)\n\n"
            f"📍 <i>Посмотреть маршрут: /find</i>"
        )
        await send_user_notification(bot, user_id, msg)

    @staticmethod
    async def notify_fuel_available(bot: Bot, user_id: int, station_name: str, fuel_type: str, address: str):
        msg = (
            f"✅ <b>Топливо снова в наличии!</b>\n\n"
            f"⛽ АЗС: <b>{station_name}</b>\n"
            f"📍 Адрес: {address}\n"
            f"⚡ Тип: <b>{fuel_type}</b> появился на колонках.\n\n"
            f"🚗 <i>Рекомендуем заправиться!</i>"
        )
        await send_user_notification(bot, user_id, msg)

async def process_price_drop_alerts(bot: Bot):
    try:
        async with AsyncSessionLocal() as db:
            stmt = text("""
                SELECT 
                    s.id AS station_id,
                    s.name AS station_name,
                    s.brand,
                    s.address,
                    s.city_id,
                    f.fuel_type,
                    f.price AS current_price
                FROM fuel_prices f
                JOIN stations s ON s.id = f.station_id
                WHERE f.price IS NOT NULL
                ORDER BY f.price ASC
                LIMIT 10;
            """)
            drops = (await db.execute(stmt)).mappings().all()
            if not drops:
                return
            user_stmt = text("""
                SELECT telegram_id 
                FROM users 
                WHERE telegram_id IS NOT NULL
                LIMIT 50;
            """)
            users = (await db.execute(user_stmt)).mappings().all()
            if users and drops:
                best = drops[0]
                msg = (
                    f"⚡ <b>Выгодная цена на топливо!</b>\n\n"
                    f"⛽ <b>{best['brand']}</b> ({best['address'] or 'АЗС'})\n"
                    f"🔹 Марка: <b>{best['fuel_type']}</b>\n"
                    f"🔥 Цена: <b>{float(best['current_price']):.2f} ₽</b>\n\n"
                    f"<i>Смотрите все АЗС рядом: /find</i>"
                )
                user_ids = [u["telegram_id"] for u in users]
                await safe_broadcast(bot, user_ids, msg)
    except Exception as e:
        logger.warning(f"[PriceAlerts] Предупреждение при проверке цен: {e}")

async def price_alert_worker(bot: Bot):
    logger.info("[PriceAlertWorker] Сервис мониторинга цен запущен.")
    await asyncio.sleep(60)
    while True:
        try:
            await process_price_drop_alerts(bot)
        except asyncio.CancelledError:
            logger.info("[PriceAlertWorker] Воркер мониторинга цен остановлен.")
            break
        except Exception as e:
            logger.error(f"[PriceAlertWorker] Необработанное исключение: {e}")
        await asyncio.sleep(1800)

send_price_alerts = process_price_drop_alerts
price_alerts_worker = price_alert_worker
