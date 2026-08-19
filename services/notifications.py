import logging
from datetime import datetime, timezone
from database.session import AsyncSessionLocal
from database.crud import (
    get_all_active_notifications, get_latest_price, get_latest_availability,
    deactivate_notification, update_notification_last_triggered
)
from database.models import AvailabilityStatus
from utils.helpers import format_time_ago
from aiogram import Bot
from config import settings

logger = logging.getLogger(__name__)
bot = Bot(token=settings.BOT_TOKEN)

async def check_notifications():
    logger.info("Проверка уведомлений...")
    async with AsyncSessionLocal() as db:
        notifications = await get_all_active_notifications(db)
        for notif in notifications:
            if notif.notify_on_low_price and notif.last_triggered_at:
                last = notif.last_triggered_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if (datetime.now(timezone.utc) - last).total_seconds() < 86400:
                    continue
            fuel = notif.fuel_type
            station = notif.station
            if station:
                price = await get_latest_price(db, station.id, fuel)
                avail = await get_latest_availability(db, station.id, fuel)
            else:
                continue

            triggered = False
            if notif.target_price and price and price.price <= notif.target_price:
                triggered = True
                text = (
                    f"🔔 Цена достигла вашего уровня!\n"
                    f"⛽ {station.name}\n"
                    f"💰 {price.price:.2f} ₽\n"
                    f"🕐 {format_time_ago(price.recorded_at)}"
                )
            elif notif.notify_on_availability and avail and avail.status == AvailabilityStatus.GREEN:
                triggered = True
                text = (
                    f"🔔 На АЗС {station.name} появилось топливо {fuel.value}!\n"
                    f"🟢 Наличие подтверждено {format_time_ago(avail.recorded_at)}"
                )
            if triggered:
                try:
                    await bot.send_message(notif.user.telegram_id, text)
                    if notif.target_price:
                        await deactivate_notification(db, notif.id)
                    else:
                        await update_notification_last_triggered(db, notif.id)
                    logger.info(f"Уведомление отправлено пользователю {notif.user.telegram_id}")
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления: {e}")
