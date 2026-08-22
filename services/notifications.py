import logging
from datetime import datetime, timezone, timedelta
from database.session import AsyncSessionLocal
from database.crud import (
    get_all_active_notifications, get_latest_price, get_latest_availability,
    deactivate_notification, update_notification_last_triggered
)
from database.models import AvailabilityStatus, Station
from utils.helpers import format_time_ago, haversine_distance
from utils.time_utils import ensure_utc
from aiogram import Bot
from config import settings

logger = logging.getLogger(__name__)
bot = Bot(token=settings.BOT_TOKEN)

async def check_notifications():
    logger.info("Проверка уведомлений...")
    async with AsyncSessionLocal() as db:
        notifications = await get_all_active_notifications(db)
        for notif in notifications:
            # Уведомления о цене
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
                            await bot.send_message(
                                notif.user.telegram_id,
                                f"🔔 Цена достигла вашего уровня!\n"
                                f"⛽ {station.name}\n"
                                f"💰 {price.price:.2f} ₽\n"
                                f"🕐 {format_time_ago(price.recorded_at)}"
                            )
                            await deactivate_notification(db, notif.id)
                            logger.info(f"Уведомление о цене отправлено {notif.user.telegram_id}")
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления: {e}")
                        continue

            # Уведомления о наличии (на конкретной АЗС или в радиусе)
            if notif.notify_on_availability:
                station = notif.station
                if station:
                    avail = await get_latest_availability(db, station.id, notif.fuel_type)
                    if avail and avail.status == AvailabilityStatus.GREEN:
                        if notif.last_triggered_at:
                            last = ensure_utc(notif.last_triggered_at)
                            if (datetime.now(timezone.utc) - last).total_seconds() < 3600:
                                continue
                        try:
                            await bot.send_message(
                                notif.user.telegram_id,
                                f"🔔 На АЗС {station.name} появилось топливо {notif.fuel_type.value}!\n"
                                f"🟢 Наличие подтверждено {format_time_ago(avail.recorded_at)}"
                            )
                            await update_notification_last_triggered(db, notif.id)
                            logger.info(f"Уведомление о наличии отправлено {notif.user.telegram_id}")
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления: {e}")
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
                                    continue
                            try:
                                await bot.send_message(
                                    user.telegram_id,
                                    f"🔔 В радиусе {notif.radius_km} км появилось топливо {notif.fuel_type.value} на АЗС {st.name}! 🟢"
                                )
                                await update_notification_last_triggered(db, notif.id)
                                logger.info(f"Уведомление по радиусу отправлено {user.telegram_id}")
                            except Exception as e:
                                logger.error(f"Ошибка отправки уведомления: {e}")
                            break
