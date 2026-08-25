import logging
from datetime import datetime, timezone, timedelta
from database.session import AsyncSessionLocal
from database.crud import (
    get_all_active_notifications, get_latest_price, get_latest_availability,
    deactivate_notification, update_notification_last_triggered,
    is_silent_hours_now
)
from database.models import AvailabilityStatus, Station
from utils.helpers import format_time_ago, haversine_distance
from utils.time_utils import ensure_utc
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import settings
from sqlalchemy import select

logger = logging.getLogger(__name__)
bot = Bot(token=settings.BOT_TOKEN)

async def check_notifications():
    logger.info("Проверка уведомлений...")
    async with AsyncSessionLocal() as db:
        notifications = await get_all_active_notifications(db)
        for notif in notifications:
            # Проверка тишины
            if await is_silent_hours_now(db, notif.user.id):
                logger.info(f"Уведомление для пользователя {notif.user.id} пропущено из-за тишины")
                continue

            # ---- Уведомления о снижении цены ----
            if notif.notify_on_low_price and notif.target_price:
                if notif.last_triggered_at:
                    last = ensure_utc(notif.last_triggered_at)
                    if (datetime.now(timezone.utc) - last).total_seconds() < 86400:
                        continue
                station = notif.station
                if station:
                    price = await get_latest_price(db, station.id, notif.fuel_type)
                    if price and price.price <= notif.target_price:
                        try:
                            kb = InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="❌ Отписаться", callback_data=f"unsub_{notif.id}")]
                            ])
                            await bot.send_message(
                                notif.user.telegram_id,
                                f"📉 Цена на АЗС «{station.name}» упала до {price.price:.2f} ₽ (было выше).\n"
                                f"Экономия: до {round((notif.target_price - price.price), 2)} ₽ за литр.\n"
                                f"Заправляйся сейчас! 🚗",
                                reply_markup=kb
                            )
                            await deactivate_notification(db, notif.id)
                            logger.info(f"Уведомление о цене отправлено {notif.user.telegram_id}")
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления: {e}")
                        continue

            # ---- Уведомления о появлении топлива (конкретная АЗС) ----
            if notif.notify_on_availability:
                station = notif.station
                if station:
                    avail = await get_latest_availability(db, station.id, notif.fuel_type)
                    if not avail:
                        continue
                    if avail.status == AvailabilityStatus.GREEN:
                        should_send = False
                        if notif.last_triggered_at is None:
                            should_send = True
                        else:
                            last = ensure_utc(notif.last_triggered_at)
                            if ensure_utc(avail.recorded_at) > last:
                                should_send = True
                        if should_send:
                            try:
                                kb = InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="❌ Отписаться", callback_data=f"unsub_{notif.id}")]
                                ])
                                await bot.send_message(
                                    notif.user.telegram_id,
                                    f"⛽ На АЗС «{station.name}» появился 95-й бензин!\n"
                                    f"Цена: {await get_latest_price(db, station.id, notif.fuel_type).price:.2f} ₽\n"
                                    f"Адрес: {station.address}\n\n"
                                    f"Успей, пока не разобрали!",
                                    reply_markup=kb
                                )
                                await update_notification_last_triggered(db, notif.id)
                                logger.info(f"Уведомление о наличии отправлено {notif.user.telegram_id}")
                            except Exception as e:
                                logger.error(f"Ошибка отправки уведомления: {e}")

                # ---- Уведомления по радиусу (если есть) ----
                elif notif.radius_km is not None:
                    user = notif.user
                    city = user.city
                    if not city or city.latitude is None:
                        continue
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
                    stations_with_green = await db.execute(
                        select(Station)
                        .join(AvailabilityReport)
                        .where(
                            AvailabilityReport.is_fresh == True,
                            AvailabilityReport.recorded_at >= cutoff,
                            AvailabilityReport.status == AvailabilityStatus.GREEN,
                            AvailabilityReport.fuel_type == notif.fuel_type,
                            Station.city_id == city.id,
                            Station.is_active == True
                        )
                        .group_by(Station.id)
                    )
                    stations = stations_with_green.scalars().all()
                    user_lat = city.latitude
                    user_lon = city.longitude
                    for st in stations:
                        dist = haversine_distance(user_lat, user_lon, st.latitude, st.longitude)
                        if dist <= notif.radius_km:
                            if notif.last_triggered_at:
                                last = ensure_utc(notif.last_triggered_at)
                                if (datetime.now(timezone.utc) - last).total_seconds() < 3600:
                                    break
                            try:
                                kb = InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="❌ Отписаться", callback_data=f"unsub_{notif.id}")]
                                ])
                                await bot.send_message(
                                    user.telegram_id,
                                    f"🔔 В радиусе {notif.radius_km} км появилось топливо {notif.fuel_type.value} на АЗС {st.name}! 🟢",
                                    reply_markup=kb
                                )
                                await update_notification_last_triggered(db, notif.id)
                                logger.info(f"Уведомление по радиусу отправлено {user.telegram_id}")
                            except Exception as e:
                                logger.error(f"Ошибка отправки уведомления: {e}")
                            break
