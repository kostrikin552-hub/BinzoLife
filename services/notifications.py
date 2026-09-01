import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any, Union
from aiogram import Bot
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# =====================================================================
# 1. УТИЛИТЫ ОТПРАВКИ И УПРАВЛЕНИЯ УВЕДОМЛЕНИЯМИ
# =====================================================================

async def send_user_notification(
    bot: Bot,
    telegram_id: int,
    message: str,
    keyboard: Optional[Any] = None,
    parse_mode: str = "HTML"
) -> bool:
    """Безопасная отправка уведомления пользователю с перехватом блокировок."""
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


async def broadcast_admin_alert(bot: Bot, message: str, admin_ids: Optional[List[int]] = None):
    """Рассылка системного алерта администраторам бота."""
    if not admin_ids:
        try:
            from config import ADMIN_IDS
            admin_ids = ADMIN_IDS if isinstance(ADMIN_IDS, list) else [ADMIN_IDS]
        except Exception:
            admin_ids = []

    for admin_id in admin_ids:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=f"🔔 <b>[Системное уведомление BinzoLife]</b>\n\n{message}",
                parse_mode="HTML"
            )
            await asyncio.sleep(0.05)
        except Exception:
            pass


class NotificationService:
    """Класс-сервис для вызова из любых хендлеров и сервисов."""

    @staticmethod
    async def notify_price_drop(
        bot: Bot,
        user_id: int,
        station_name: str,
        fuel_type: str,
        old_price: float,
        new_price: float
    ):
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
    async def notify_fuel_available(
        bot: Bot,
        user_id: int,
        station_name: str,
        fuel_type: str,
        address: str
    ):
        msg = (
            f"✅ <b>Топливо снова в наличии!</b>\n\n"
            f"⛽ АЗС: <b>{station_name}</b>\n"
            f"📍 Адрес: {address}\n"
            f"⚡ Тип: <b>{fuel_type}</b> появился на колонках.\n\n"
            f"🚗 <i>Рекомендуем заправиться без очереди!</i>"
        )
        await send_user_notification(bot, user_id, msg)


# =====================================================================
# 2. ЛОГИКА ОТСЛЕЖИВАНИЯ ЦЕН И РАССЫЛКИ АЛЕРТОВ
# =====================================================================

async def process_price_drop_alerts(bot: Bot):
    """
    Проверяет снижение цен (>= 0.30 ₽) за последние 6 часов 
    и отправляет персональные уведомления пользователям города.
    """
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
                    f.price AS current_price,
                    f.previous_price
                FROM fuel_prices f
                JOIN stations s ON s.id = f.station_id
                WHERE f.is_fresh = true
                  AND f.previous_price IS NOT NULL
                  AND f.price < f.previous_price
                  AND (f.previous_price - f.price) >= 0.30
                  AND f.updated_at >= NOW() - INTERVAL '6 hours'
                LIMIT 50;
            """)
            drops = (await db.execute(stmt)).mappings().all()

            if not drops:
                return

            for drop in drops:
                city_id = drop["city_id"]
                fuel_type = drop["fuel_type"]
                diff = float(drop["previous_price"]) - float(drop["current_price"])

                user_stmt = text("""
                    SELECT telegram_id 
                    FROM users 
                    WHERE is_active = true 
                      AND (city_id = :city_id OR :city_id IS NULL)
                    LIMIT 200;
                """)
                users = (await db.execute(user_stmt, {"city_id": city_id})).mappings().all()

                msg = (
                    f"⚡ <b>Снижение цены в вашем городе!</b>\n\n"
                    f"⛽ <b>{drop['brand']}</b> ({drop['address']})\n"
                    f"🔹 Марка: <b>{fuel_type}</b>\n"
                    f"📉 Цена упала: <s>{drop['previous_price']:.2f} ₽</s> ➔ <b>{drop['current_price']:.2f} ₽</b>\n"
                    f"💸 Экономия: <b>{diff:.2f} ₽/л</b>\n\n"
                    f"<i>Смотрите маршрут командой /find</i>"
                )

                for u in users:
                    await send_user_notification(bot, u["telegram_id"], msg)
                    await asyncio.sleep(0.05)

    except Exception as e:
        logger.error(f"[PriceAlerts] Ошибка выборки снижения цен: {e}")


# =====================================================================
# 3. ФОНОВЫЙ ВОРКЕР ДЛЯ MAIN.PY
# =====================================================================

async def price_alert_worker(bot: Bot):
    """
    Фоновый воркер мониторинга и рассылки алертов цен (раз в 30 минут).
    """
    logger.info("[PriceAlertWorker] Сервис мониторинга цен успешно запущен.")
    await asyncio.sleep(45)
    while True:
        try:
            await process_price_drop_alerts(bot)
        except asyncio.CancelledError:
            logger.info("[PriceAlertWorker] Воркер мониторинга цен остановлен.")
            break
        except Exception as e:
            logger.error(f"[PriceAlertWorker] Необработанное исключение: {e}")

        await asyncio.sleep(1800)


# Алиасы
send_price_alerts = process_price_drop_alerts
price_alerts_worker = price_alert_worker
