import logging
from datetime import datetime, timezone, timedelta
from database.session import AsyncSessionLocal
from database.crud import (
    get_all_active_notifications, get_latest_price, get_latest_availability,
    deactivate_notification, update_notification_last_triggered,
    get_notification_by_id
)
from database.models import AvailabilityStatus, Station
from utils.helpers import format_time_ago, haversine_distance
from utils.time_utils import ensure_utc
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import settings

logger = logging.getLogger(__name__)
bot = Bot(token=settings.BOT_TOKEN)

async def check_notifications():
    logger.info("Проверка уведомлений...")
    async with AsyncSessionLocal() as db:
        notifications = await get_all_active_notifications(db)
        for notif in notifications:
            # ---- Уведомления о цене ----
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
                                f"🔔 Цена достигла вашего уровня!\n"
                                f"⛽ {station.name}\n"
                                f"💰 {price.price:.2f} ₽\n"
                                f"🕐 {format_time_ago(price.recorded_at)}",
                                reply_markup=kb
                            )
                            await deactivate_notification(db, notif.id)
                            logger.info(f"Уведомление о цене отправлено {notif.user.telegram_id}")
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления: {e}")
                        continue

            # ---- Уведомления о наличии (конкретная АЗС или радиус) ----
            if notif.notify_on_availability:
                station = notif.station
                if station:
                    # Получаем последний отчёт о наличии
                    avail = await get_latest_availability(db, station.id, notif.fuel_type)
                    if not avail:
                        continue

                    # Условие отправки: статус GREEN И (либо last_triggered_at не установлен, либо время отчёта новее)
                    if avail.status == AvailabilityStatus.GREEN:
                        should_send = False
                        if notif.last_triggered_at is None:
                            should_send = True
                        else:
                            last = ensure_utc(notif.last_triggered_at)
                            # Если время отчёта больше last_triggered, значит это новое изменение
                            if ensure_utc(avail.recorded_at) > last:
                                should_send = True

                        if should_send:
                            try:
                                kb = InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="❌ Отписаться", callback_data=f"unsub_{notif.id}")]
                                ])
                                await bot.send_message(
                                    notif.user.telegram_id,
                                    f"🔔 На АЗС {station.name} появилось топливо {notif.fuel_type.value}!\n"
                                    f"🟢 Наличие подтверждено {format_time_ago(avail.recorded_at)}",
                                    reply_markup=kb
                                )
                                await update_notification_last_triggered(db, notif.id)
                                logger.info(f"Уведомление о наличии отправлено {notif.user.telegram_id}")
                            except Exception as e:
                                logger.error(f"Ошибка отправки уведомления: {e}")
                    # Если статус не GREEN, но last_triggered был установлен, можно сбросить, чтобы при следующем GREEN отправить
                    # Но мы не сбрасываем, так как при новом GREEN условие should_send сработает (время отчёта > last_triggered)
                elif notif.radius_km is not None:
                    # Уведомления по радиусу – аналогичная логика
                    user = notif.user
                    city = user.city
                    if not city or city.latitude is None:
                        continue
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
                    # Находим станции с GREEN, у которых отчёт свежий (is_fresh)
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
                    # Проверяем каждую станцию в радиусе
                    for st in stations:
                        dist = haversine_distance(user_lat, user_lon, st.latitude, st.longitude)
                        if dist <= notif.radius_km:
                            # Нашли станцию в радиусе
                            # Проверяем, не отправляли ли уже недавно (по last_triggered)
                            if notif.last_triggered_at:
                                last = ensure_utc(notif.last_triggered_at)
                                # Если прошло меньше часа, не отправляем повторно
                                if (datetime.now(timezone.utc) - last).total_seconds() < 3600:
                                    break
                            # Отправляем уведомление
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
                            break  # отправляем только одно уведомление за раз
