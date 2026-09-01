import html
import logging
import io
import qrcode
from datetime import datetime, timedelta, timezone, date
from PIL import Image, ImageDraw, ImageFont
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from sqlalchemy import select, func, update, text

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_user, log_action, get_city_by_id, get_station_by_id,
    get_latest_price, create_notification, save_price,
    get_latest_fresh_price, get_avg_price_30d, get_min_price_30d, get_max_price_30d,
    set_first_search, get_active_notifications_for_user,
    activate_trial, increment_station_views, get_referral_link,
    save_availability_report_with_consensus,
    get_cached_address, cache_address,
    can_use_free_search, use_free_search,
    get_stations_in_radius, get_user_by_id, add_free_pro_days
)
from database.models import FuelType, AvailabilityStatus, SourceType, Station, FuelPrice, AvailabilityReport, UserAction, Referral
from services.rating import calculate_rating
from services.subscription import check_pro
from services.graphics import generate_price_graph
from utils.helpers import status_emoji, format_time_ago, haversine_distance
from utils.cleaners import clean_address, is_likely_address
from utils.geocoder import reverse_geocode
from keyboards.reply import main_menu_keyboard, fuel_choice_keyboard
from keyboards.inline import sort_choice_keyboard, station_action_keyboard, pro_purchase_keyboard

logger = logging.getLogger(__name__)
router = Router()

class FindStates(StatesGroup):
    choosing_fuel = State()
    choosing_sort = State()

class ReportPriceStates(StatesGroup):
    waiting_price = State()

# ========== КАЛЬКУЛЯТОР ЧИСТОЙ ВЫГОДЫ ==========
def calculate_true_savings(station_price: float, avg_city_price: float, distance_km: float,
                           tank_volume: float = 50, consumption_per_100km: float = 9) -> dict:
    if avg_city_price <= 0 or station_price <= 0:
        return {"gross_savings": 0, "trip_cost": 0, "net_savings": 0, "is_worth": False, "badge": ""}
    gross_savings = max(0, (avg_city_price - station_price) * tank_volume)
    round_trip_km = distance_km * 2
    trip_fuel_liters = (round_trip_km / 100) * consumption_per_100km
    trip_cost = round(trip_fuel_liters * station_price)
    net_savings = round(gross_savings - trip_cost)
    is_worth = net_savings > 50

    if is_worth:
        badge = f"✅ Чистый профит: +{net_savings} ₽ (с учётом дороги)"
    else:
        badge = f"⚠️ Не выгодно: дорога съест {trip_cost} ₽"
    return {
        "gross_savings": gross_savings,
        "trip_cost": trip_cost,
        "net_savings": net_savings,
        "is_worth": is_worth,
        "badge": badge
    }
# ===============================================

# ========== ПОЛУЧЕНИЕ СТАТУСА ТОПЛИВА ИЗ station_current_fuel ==========
async def get_fuel_status(db, station_id: int, fuel_type: FuelType) -> dict:
    """Получает актуальный статус топлива для станции из station_current_fuel."""
    result = await db.execute(
        text("""
            SELECT price, availability, queue_level, observed_at, source
            FROM station_current_fuel
            WHERE station_id = :station_id AND fuel_type = :fuel_type
            ORDER BY observed_at DESC
            LIMIT 1
        """),
        {"station_id": station_id, "fuel_type": fuel_type.value}
    )
    row = result.fetchone()
    if not row:
        return {
            "price": None,
            "availability": "unknown",
            "queue_level": "unknown",
            "observed_at": None,
            "source": None,
        }
    return {
        "price": row.price,
        "availability": row.availability,
        "queue_level": row.queue_level,
        "observed_at": row.observed_at,
        "source": row.source,
    }
# ===============================================================

# ---------- Генерация картинки для шеринга ----------
async def generate_share_image(name: str, price: float, status: str, address: str, ref_link: str) -> bytes:
    try:
        img = Image.new('RGB', (800, 400), color='#1a1a2e')
        draw = ImageDraw.Draw(img)
        try:
            font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
            font_text = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        except:
            font_title = ImageFont.load_default()
            font_text = ImageFont.load_default()

        draw.text((50, 50), f"⛽ {name}", fill='white', font=font_title)
        draw.text((50, 120), f"Цена: {price} ₽/л", fill='#4caf50', font=font_text)
        draw.text((50, 170), f"Наличие: {status}", fill='#ffeb3b', font=font_text)
        draw.text((50, 220), f"Адрес: {address[:50]}", fill='#bbdefb', font=font_text)

        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(ref_link)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_img = qr_img.resize((150, 150))
        img.paste(qr_img, (600, 200))

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Ошибка генерации картинки: {e}")
        return None

# ---------- Старт поиска ----------
@router.message(F.text == "⛽ Найти заправку")
async def start_find(message: types.Message, state: FSMContext):
    await state.set_state(FindStates.choosing_fuel)
    await message.answer("Выберите вид топлива:", reply_markup=fuel_choice_keyboard())

# ---------- Выбор топлива ----------
@router.message(FindStates.choosing_fuel, F.text.in_(["⛽ АИ-92", "⛽ АИ-95", "⛽ АИ-98", "⛽ АИ-100", "⛽ ДТ"]))
async def choose_fuel(message: types.Message, state: FSMContext):
    fuel_map = {
        "⛽ АИ-92": FuelType.AI_92,
        "⛽ АИ-95": FuelType.AI_95,
        "⛽ АИ-98": FuelType.AI_98,
        "⛽ АИ-100": FuelType.AI_100,
        "⛽ ДТ": FuelType.DT,
    }
    fuel_type = fuel_map.get(message.text)
    if not fuel_type:
        await message.answer("Пожалуйста, выберите топливо из списка.")
        return

    await state.update_data(fuel_type=fuel_type)
    logger.info(f"Выбрано топливо: {fuel_type.value}")

    # Проверяем, есть ли город у пользователя
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            user = await create_user(db, message.from_user.id, message.from_user.username)
            logger.info(f"Создан новый пользователь {user.telegram_id} в процессе выбора топлива")

        if not user.city_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 Перейти в профиль", callback_data="go_profile")]
            ])
            await message.answer(
                "❌ Город не выбран. Пожалуйста, установите город в профиле.",
                reply_markup=kb
            )
            await state.clear()
            return

        city = await get_city_by_id(db, user.city_id)
        if not city or city.latitude is None or city.longitude is None:
            await message.answer(
                "❌ У выбранного города не заданы координаты. Обратитесь к администратору.",
                reply_markup=main_menu_keyboard()
            )
            await state.clear()
            return

        await state.update_data(city_id=city.id, lat=city.latitude, lon=city.longitude)

    await state.set_state(FindStates.choosing_sort)
    await message.answer(
        "Как отсортировать результаты?\n\n"
        "🔥 По рейтингу (баланс цены и наличия)\n"
        "💰 По минимальной цене",
        reply_markup=sort_choice_keyboard()
    )

@router.message(FindStates.choosing_fuel, F.text == "◀️ Назад")
async def back_to_menu_from_fuel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())

@router.message(FindStates.choosing_fuel)
async def handle_unknown_in_choosing_fuel(message: types.Message, state: FSMContext):
    await message.answer("Пожалуйста, воспользуйтесь кнопками ниже.", reply_markup=fuel_choice_keyboard())

# ---------- Обработчики выбора сортировки ----------
@router.callback_query(F.data == "sort_rating")
async def sort_rating(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(sort_mode="rating")
    await perform_search(callback.message, state)

@router.callback_query(F.data == "sort_price")
async def sort_price(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(sort_mode="price")
    await perform_search(callback.message, state)

# ---------- Основная функция поиска ----------
async def perform_search(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        city_id = data.get("city_id")
        fuel_type = data.get("fuel_type", FuelType.AI_95)
        sort_mode = data.get("sort_mode", "rating")

        if not city_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 Перейти в профиль", callback_data="go_profile")]
            ])
            await message.answer("❌ Город не выбран. Пожалуйста, установите город в профиле.", reply_markup=kb)
            await state.clear()
            return

        async with AsyncSessionLocal() as db:
            user = await get_user(db, message.from_user.id)
            if not user:
                user = await create_user(db, message.from_user.id, message.from_user.username)
                logger.info(f"Создан новый пользователь {user.telegram_id} во время поиска")

            if not user.city_id and city_id:
                city = await get_city_by_id(db, city_id)
                if city:
                    user.city_id = city_id
                    await db.commit()
                else:
                    await message.answer("❌ Город не найден. Пожалуйста, выберите город в профиле.")
                    await state.clear()
                    return

            if not user.city_id:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👤 Перейти в профиль", callback_data="go_profile")]
                ])
                await message.answer(
                    "❌ Город не выбран. Пожалуйста, установите город в профиле.",
                    reply_markup=kb
                )
                await state.clear()
                return

            city = await get_city_by_id(db, user.city_id)
            if not city or city.latitude is None or city.longitude is None:
                await message.answer(
                    "❌ У выбранного города не заданы координаты. Обратитесь к администратору.",
                    reply_markup=main_menu_keyboard()
                )
                await state.clear()
                return

            city_lat = city.latitude
            city_lon = city.longitude
            logger.info(f"Город пользователя: {city.name}, координаты: lat={city_lat}, lon={city_lon}")

            is_pro = await check_pro(user.telegram_id)
            logger.info(f"🔍 check_pro для user {user.telegram_id} вернул: {is_pro}")
            if not is_pro:
                logger.info(f"📋 Данные пользователя: is_pro={user.is_pro}, pro_until={user.pro_until}")
                can_search = await can_use_free_search(db, user.id)
                if not can_search:
                    await message.answer(
                        "🛑 Бесплатный поиск на сегодня завершён.\n\n"
                        "👑 Подключи **PRO** и получи:\n"
                        "🔹 Безлимитный поиск с расчётом чистой выгоды\n"
                        "🔹 Уведомления о скачках цен рядом с домом\n"
                        "🔹 Навигатор в 1 клик (Яндекс, 2ГИС)\n\n"
                        "💡 **99 ₽/мес** — окупается с первой заправки!\n"
                        "⚡ **29 ₽/сутки** — для одной поездки\n\n"
                        "👇 Выбери тариф:",
                        reply_markup=pro_purchase_keyboard()
                    )
                    await state.clear()
                    return
                remaining = await use_free_search(db, user.id)
                logger.info(f"🔢 После использования осталось бесплатных поисков: {remaining}")

            await set_first_search(db, user.id)

            stations = await get_stations_in_radius(db, city.id, city_lat, city_lon, radius_km=10.0)

            if not stations:
                await message.answer("В этом городе пока нет АЗС или они далеко.", reply_markup=main_menu_keyboard())
                await state.clear()
                return

            station_ids = [s.id for s in stations]

            price_subq = (
                select(FuelPrice.station_id, FuelPrice.fuel_type, func.max(FuelPrice.recorded_at).label("max_date"))
                .where(
                    FuelPrice.station_id.in_(station_ids),
                    FuelPrice.fuel_type == fuel_type,
                    FuelPrice.is_fresh == True
                )
                .group_by(FuelPrice.station_id, FuelPrice.fuel_type)
                .subquery()
            )
            price_stmt = (
                select(FuelPrice)
                .join(price_subq,
                      (FuelPrice.station_id == price_subq.c.station_id) &
                      (FuelPrice.fuel_type == price_subq.c.fuel_type) &
                      (FuelPrice.recorded_at == price_subq.c.max_date))
            )
            price_result = await db.execute(price_stmt)
            prices = {p.station_id: p for p in price_result.scalars().all()}

            avail_subq = (
                select(AvailabilityReport.station_id, AvailabilityReport.fuel_type, func.max(AvailabilityReport.recorded_at).label("max_date"))
                .where(
                    AvailabilityReport.station_id.in_(station_ids),
                    AvailabilityReport.fuel_type == fuel_type,
                    AvailabilityReport.is_fresh == True
                )
                .group_by(AvailabilityReport.station_id, AvailabilityReport.fuel_type)
                .subquery()
            )
            avail_stmt = (
                select(AvailabilityReport)
                .join(avail_subq,
                      (AvailabilityReport.station_id == avail_subq.c.station_id) &
                      (AvailabilityReport.fuel_type == avail_subq.c.fuel_type) &
                      (AvailabilityReport.recorded_at == avail_subq.c.max_date))
            )
            avail_result = await db.execute(avail_stmt)
            avails = {a.station_id: a for a in avail_result.scalars().all()}

            avg_price = await get_avg_price_30d(db, city.id, fuel_type) or 0
            min_price = await get_min_price_30d(db, city.id, fuel_type) or 0
            max_price = await get_max_price_30d(db, city.id, fuel_type) or 0
            logger.info(f"Средняя цена за 30 дней для {city.name} ({fuel_type.value}): {avg_price}")

            results = []
            for station in stations:
                price_rec = prices.get(station.id)
                if not price_rec:
                    continue
                avail_rec = avails.get(station.id)
                dist = haversine_distance(city_lat, city_lon, station.latitude, station.longitude)
                if dist > 20:
                    continue
                rating_data = calculate_rating(
                    station=station,
                    price_record=price_rec,
                    availability_record=avail_rec,
                    avg_price_30d=avg_price or price_rec.price,
                    min_price_30d=min_price or price_rec.price,
                    max_price_30d=max_price or price_rec.price
                )
                results.append({
                    "station": station,
                    "price": price_rec.price,
                    "price_time": price_rec.recorded_at,
                    "availability": avail_rec.status if avail_rec else AvailabilityStatus.GRAY,
                    "availability_time": avail_rec.recorded_at if avail_rec else None,
                    "distance_km": dist,
                    "rating": rating_data["rating"],
                    "explanation": rating_data["explanation"],
                    "price_diff": rating_data.get("price_diff", 0),
                    "avg_price": rating_data.get("avg_price", 0),
                })

            if not results:
                await message.answer("Не найдено АЗС с актуальными ценами. Попробуйте позже.")
                await state.clear()
                return

            if sort_mode == "price":
                results.sort(key=lambda x: x["price"])
            else:
                results.sort(key=lambda x: x["rating"], reverse=True)

            await state.update_data(all_results=results, current_index=0, is_pro=is_pro)

            await show_station_card(message, results[0], 0, len(results), is_pro, state, fuel_type)

            await log_action(db, user.id, "search_result")

            # ===== АКТИВАЦИЯ РЕФЕРАЛЬНОГО БОНУСА =====
            if user and not user.has_made_first_search:
                user.has_made_first_search = True
                if user.referred_by:
                    referrer = await get_user_by_id(db, user.referred_by)
                    if referrer:
                        await add_free_pro_days(db, referrer, 3)
                        await db.execute(
                            update(Referral)
                            .where(
                                Referral.referred_user_id == user.id,
                                Referral.referrer_id == user.referred_by
                            )
                            .values(is_rewarded=True)
                        )
                        await db.commit()
                        try:
                            await message.bot.send_message(
                                referrer.telegram_id,
                                f"🎉 Ваш друг @{user.username or user.telegram_id} совершил первый поиск! Вы получили +3 дня PRO."
                            )
                        except Exception:
                            pass

            if user and not user.is_pro and not user.trial_used:
                await activate_trial(db, user.id)
                await message.answer(
                    "🎁 Вам начислен пробный доступ: 3 дня PRO-тарифа бесплатно!\n"
                    "Теперь вы можете пользоваться безлимитным поиском и уведомлениями о снижении цен."
                )

    except Exception as e:
        logger.error(f"Ошибка в perform_search: {e}", exc_info=True)
        await message.answer("⚠️ Произошла ошибка при поиске. Попробуйте позже.")
        await state.clear()

# ---------- Функция отображения карточки ----------
async def show_station_card(message: types.Message, result: dict, index: int, total: int, is_pro: bool, state: FSMContext, fuel_type: FuelType = None):
    try:
        if not result:
            await message.answer("Ошибка: нет данных для отображения.")
            return

        station = result.get("station")
        if not station:
            await message.answer("Ошибка: данные о станции отсутствуют.")
            return

        price = result.get("price", 0.0)
        price_time = result.get("price_time")
        availability = result.get("availability", AvailabilityStatus.GRAY)
        availability_time = result.get("availability_time")
        distance_km = result.get("distance_km", 0.0)
        rating = result.get("rating", 0)
        explanation = result.get("explanation", "")
        price_diff = result.get("price_diff", 0)
        avg_price = result.get("avg_price", 0)

        station_name = html.escape(station.name)
        fuel_type_str = fuel_type.value if fuel_type else "АИ-95"

        # ========== АДРЕС ==========
        async with AsyncSessionLocal() as db:
            raw_address = station.address or ""
            cleaned = clean_address(raw_address, max_length=255)
            if not is_likely_address(cleaned):
                cached = await get_cached_address(db, station.latitude, station.longitude)
                if cached:
                    cleaned = cached
                elif station.latitude != 0.0 and station.longitude != 0.0:
                    geo_addr = await reverse_geocode(station.latitude, station.longitude)
                    if geo_addr:
                        await cache_address(db, station.latitude, station.longitude, geo_addr)
                        cleaned = geo_addr
                        station.address = geo_addr
                        await db.commit()
            station_address = html.escape(cleaned) if cleaned else "адрес не указан"

        # Расстояние и время
        if distance_km > 0 and distance_km < 1000:
            distance_text = f"{distance_km:.1f} км"
            time_min = round(distance_km / 40 * 60)
            time_text = f"~{time_min} мин"
        else:
            distance_text = "расстояние неизвестно"
            time_text = ""

        # ========== ПОЛУЧАЕМ СТАТУС ТОПЛИВА ИЗ station_current_fuel ==========
        fuel_status = await get_fuel_status(db, station.id, fuel_type)
        # Если есть более свежая цена из статуса, можно заменить
        if fuel_status["price"] and fuel_status["price"] > 0 and fuel_status["observed_at"]:
            # Сравниваем время обновления: если статус свежее, используем его цену
            if price_time and fuel_status["observed_at"] > price_time:
                price = fuel_status["price"]
                price_time = fuel_status["observed_at"]
        # Формируем строки статуса
        availability_emoji = {
            "available": "🟢",
            "limited": "🟡",
            "unavailable": "🔴",
            "unknown": "⚪",
        }
        availability_text = {
            "available": "Есть",
            "limited": "Осталось мало",
            "unavailable": "Нет",
            "unknown": "неизвестно",
        }
        queue_text = {
            "low": "🟢 Свободно (до 3 машин)",
            "medium": "🟡 Небольшая очередь (4–8 машин)",
            "high": "🔴 Большая очередь (9+ машин)",
            "unknown": "⚪ неизвестно",
        }
        avail_emoji = availability_emoji.get(fuel_status["availability"], "⚪")
        avail_text = availability_text.get(fuel_status["availability"], "неизвестно")
        queue_str = queue_text.get(fuel_status["queue_level"], "⚪ неизвестно")
        status_observed_str = format_time_ago(fuel_status["observed_at"]) if fuel_status["observed_at"] else "неизвестно"
        source_str = fuel_status["source"] or "неизвестно"

        status_line = f"{avail_emoji} Наличие: {avail_text}"
        if fuel_status["observed_at"]:
            status_line += f" (обновлено {status_observed_str})"
        status_line += f"\n⏳ Очередь: {queue_str}"
        status_line += f"\n📡 Источник: {source_str}"
        # ====================================================

        # Убираем старый статус из availability_reports — он не используется
        # stars и rating – используем из результата
        stars = round(rating / 20, 1) if rating else 0
        stars_display = f"⭐ {stars} ({rating}/100)"

        async with AsyncSessionLocal() as db:
            views = await increment_station_views(db, station.id)
            views_text = f"🔥 Эту АЗС сегодня выбрали {views} водителей.\n" if views else ""

        text = (
            f"⛽ {station_name} {stars_display}\n\n"
            f"📍 {station_address}"
        )
        if distance_text != "расстояние неизвестно":
            text += f" · {distance_text}"
            if time_text:
                text += f"\n🚗 {time_text} на машине"
        else:
            text += f"\n📍 расстояние не определено (установите координаты города)"

        text += f"\n\n💰 Цена ({fuel_type_str}): {price:.2f} ₽/л"
        if avg_price and avg_price > 0 and abs(price_diff) > 0.01:
            if price_diff > 0:
                text += f" (на {price_diff:.2f} ₽ дешевле средней по городу)"
            elif price_diff < 0:
                text += f" (на {abs(price_diff):.2f} ₽ дороже средней по городу)"
        else:
            text += " (цена близка к средней по городу)"

        # ===== ЕДИНСТВЕННЫЙ БЛОК СТАТУСА (из station_current_fuel) =====
        text += f"\n\n{status_line}"
        
        # Обновление цены
        text += f"\n🕒 Обновлено: {price_time_str}"
        
        # Просмотры
        if views_text:
            text += f"\n{views_text}"

        # ===== СТАРЫЙ СТАТУС (availability_reports) НЕ ВЫВОДИМ =====
        # (убираем status_display)

        # ===== КАЛЬКУЛЯТОР ЧИСТОЙ ВЫГОДЫ =====
        if avg_price and avg_price > 0 and price > 0 and distance_km > 0:
            async with AsyncSessionLocal() as db:
                user = await get_user(db, message.from_user.id)
                tank_volume = user.tank_volume if user else 50
            savings_data = calculate_true_savings(
                station_price=price,
                avg_city_price=avg_price,
                distance_km=distance_km,
                tank_volume=tank_volume
            )
            if savings_data["is_worth"] and savings_data["net_savings"] > 0:
                text += f"\n\n💡 {savings_data['badge']}"
            elif savings_data["trip_cost"] > 0:
                text += f"\n\n⚠️ {savings_data['badge']}"

        # ===== ИНФОРМАЦИЯ О БЕСПЛАТНЫХ ПОИСКАХ =====
        if not is_pro:
            async with AsyncSessionLocal() as db:
                user = await get_user(db, message.from_user.id)
                if user:
                    today = date.today()
                    if user.last_free_search_date == today:
                        remaining = user.free_searches_today
                    else:
                        remaining = 1
                    text += f"\n\n🔍 Бесплатных точных поисков сегодня: {remaining} из 1"
            text += "\n—\n⚠️ Бесплатная версия даёт 1 точный поиск в день.\n"
            text += "Чтобы получать уведомления о появлении топлива и снижении цены в реальном времени — подключи PRO за 99 ₽/мес."

        keyboard = station_action_keyboard(
            station_id=station.id,
            price=price,
            availability=availability,
            lat=station.latitude,
            lon=station.longitude,
            city_id=station.city_id,
            is_pro=is_pro,
            index=index,
            total=total,
            fuel_type=fuel_type_str
        )

        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
        logger.info("=== КАРТОЧКА ОТПРАВЛЕНА ===")

    except Exception as e:
        logger.error(f"Ошибка при отправке карточки: {e}", exc_info=True)
        await message.answer("Произошла ошибка при отправке карточки. Попробуйте позже.")

# ---------- Обработчик "Показать ещё 2 варианта" ----------
@router.callback_query(lambda c: c.data.startswith("more_"))
async def show_more(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        data = await state.get_data()
        all_results = data.get("all_results", [])
        current_index = data.get("current_index", 0)
        is_pro = data.get("is_pro", False)

        if not all_results or current_index + 1 >= len(all_results):
            await callback.message.answer("Это все доступные варианты.")
            return

        next_index = current_index + 1
        more_results = all_results[next_index:next_index+2]

        text = "Вот ещё две АЗС с хорошими ценами:\n\n"
        for i, res in enumerate(more_results, start=next_index+1):
            station = res.get("station")
            if not station:
                continue
            price = res.get("price", 0.0)
            distance = res.get("distance_km", 0.0)
            availability = res.get("availability", AvailabilityStatus.GRAY)
            status = availability.value if availability else "GRAY"
            station_name = html.escape(station.name)

            if station.latitude and station.longitude:
                map_link = f"<a href='https://yandex.ru/maps/?pt={station.longitude},{station.latitude}&z=15'>Маршрут</a>"
            else:
                map_link = "Координаты отсутствуют"

            dist_text = f"{distance:.1f} км" if distance > 0 else "расстояние неизвестно"
            text += (
                f"{i}. {station_name} — {price:.2f} ₽/л, {status}, {dist_text}\n"
                f"   🗺 {map_link}\n\n"
            )

        await state.update_data(current_index=next_index+1)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад к лучшей", callback_data="back_to_best")],
            [InlineKeyboardButton(text="🔄 Найти заново", callback_data="restart_search")]
        ])
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в show_more: {e}", exc_info=True)
        await callback.message.answer("Произошла ошибка. Попробуйте начать поиск заново.")

@router.callback_query(F.data == "back_to_best")
async def back_to_best(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        data = await state.get_data()
        all_results = data.get("all_results", [])
        is_pro = data.get("is_pro", False)
        if all_results:
            await show_station_card(callback.message, all_results[0], 0, len(all_results), is_pro, state)
        else:
            await callback.message.answer("Нет сохранённых результатов.", reply_markup=main_menu_keyboard())
            await state.clear()
    except Exception as e:
        logger.error(f"Ошибка в back_to_best: {e}", exc_info=True)
        await callback.message.answer("Произошла ошибка. Попробуйте начать поиск заново.")
        await state.clear()

@router.callback_query(F.data == "restart_search")
async def restart_search(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await start_find(callback.message, state)

@router.callback_query(F.data == "go_profile")
async def go_profile_callback(callback: types.CallbackQuery):
    await callback.answer()
    from handlers.profile import show_profile
    await show_profile(callback.message)

# ---------- Следить за ценой (PRO) ----------
@router.callback_query(lambda c: c.data.startswith("follow_"))
async def follow_price(callback: types.CallbackQuery):
    logger.info(f"[CALLBACK] follow_ вызван: {callback.data}")
    await callback.answer()
    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return
    try:
        station_id = int(parts[1])
        fuel_code = "_".join(parts[2:]) if len(parts) > 2 else "AI-95"
        fuel_type = FuelType(fuel_code)
    except (IndexError, ValueError, KeyError) as e:
        logger.error(f"Ошибка парсинга: {e}")
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return
    fuel_type_str = fuel_type.value

    if not await check_pro(callback.from_user.id):
        await callback.message.answer(
            "⛔ <b>Уведомления о снижении цены доступны только для PRO-подписчиков</b>\n\n"
            "💎 Оформите PRO за 99 ₽/месяц и получайте оповещения:\n"
            "• Когда цена упадёт ниже заданного уровня\n"
            "• Когда на вашей АЗС появится бензин\n"
            "• Графики цен и аналитику\n\n"
            "Нажмите на кнопку «💎 PRO» в главном меню, чтобы оплатить.",
            reply_markup=pro_purchase_keyboard()
        )
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.message.answer("Сначала выполните /start")
            return
        station = await get_station_by_id(db, station_id)
        if not station:
            await callback.message.answer("АЗС не найдена.")
            return

        existing = await get_active_notifications_for_user(db, user.id)
        for n in existing:
            if n.station_id == station_id and n.fuel_type == fuel_type and n.notify_on_low_price:
                await callback.message.answer(f"Вы уже подписаны на эту АЗС на снижение цены ({fuel_type_str}).")
                return

        latest_price = await get_latest_fresh_price(db, station_id, fuel_type)
        if not latest_price:
            await callback.message.answer(f"Не удалось получить текущую цену для {fuel_type_str}.")
            return
        target_price = round(latest_price.price - 0.5, 2)
        if target_price < 0:
            target_price = 0

        await create_notification(
            db,
            user_id=user.id,
            fuel_type=fuel_type,
            station_id=station_id,
            target_price=target_price,
            notify_on_low_price=True
        )
        await callback.message.answer(
            f"✅ Подписка на цену на АЗС <b>{station.name}</b> ({fuel_type_str}) активирована.\n"
            f"Я сообщу, когда цена станет ≤ {target_price} ₽.\n"
            f"(Отписаться можно в разделе «Мои уведомления».)"
        )

# ---------- Уведомления о появлении (PRO) ----------
@router.callback_query(lambda c: c.data.startswith("alert_avail_"))
async def subscribe_availability(callback: types.CallbackQuery):
    logger.info(f"[CALLBACK] alert_avail_ вызван: {callback.data}")
    await callback.answer()

    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return
    try:
        station_id = int(parts[2])
        fuel_code = "_".join(parts[3:]) if len(parts) > 3 else "AI-95"
        fuel_type = FuelType(fuel_code)
    except (IndexError, ValueError, KeyError) as e:
        logger.error(f"Ошибка парсинга: {e}")
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return
    fuel_type_str = fuel_type.value

    if not await check_pro(callback.from_user.id):
        await callback.message.answer(
            "⛔ <b>Уведомления о появлении топлива доступны только для PRO-подписчиков</b>\n\n"
            "💎 Оформите PRO за 99 ₽/месяц и получайте оповещения:\n"
            "• Когда на вашей АЗС появится бензин\n"
            "• Когда цена упадёт ниже целевого уровня\n\n"
            "Нажмите на кнопку «💎 PRO» в главном меню, чтобы оплатить.",
            reply_markup=pro_purchase_keyboard()
        )
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.message.answer("Сначала /start")
            return
        station = await get_station_by_id(db, station_id)
        if not station:
            await callback.answer("АЗС не найдена")
            return

        existing = await get_active_notifications_for_user(db, user.id)
        for n in existing:
            if n.station_id == station_id and n.fuel_type == fuel_type and n.notify_on_availability:
                await callback.message.answer(f"Вы уже подписаны на уведомления о появлении {fuel_type_str} на этой АЗС.")
                return

        await create_notification(
            db,
            user_id=user.id,
            fuel_type=fuel_type,
            station_id=station_id,
            notify_on_availability=True
        )
        await db.commit()

    await callback.answer(f"Вы подписаны на уведомления о появлении {fuel_type_str} на этой АЗС")
    await callback.message.answer(f"🔔 Вы будете получать уведомления, когда на {station.name} появится {fuel_type_str}.")

# ---------- Сообщить цену ----------
@router.callback_query(lambda c: c.data.startswith("report_price_"))
async def start_report_price(callback: types.CallbackQuery, state: FSMContext):
    logger.info(f"[CALLBACK] report_price_ вызван: {callback.data}")
    await callback.answer()
    try:
        station_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError) as e:
        logger.error(f"Ошибка парсинга station_id из {callback.data}: {e}")
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return
    await state.update_data(report_station_id=station_id)
    await state.set_state(ReportPriceStates.waiting_price)
    await callback.message.answer(
        "✏️ Введите актуальную цену на этой АЗС (в рублях за литр, например, 68.50):\n\n"
        "(Если хотите сообщить статус наличия, добавьте через пробел: цена статус, например: 68.50 GREEN)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
        ])
    )

@router.message(ReportPriceStates.waiting_price, F.text)
async def process_report_price(message: types.Message, state: FSMContext):
    parts = message.text.strip().split()
    try:
        price = float(parts[0].replace(',', '.'))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Пожалуйста, введите положительное число (например, 68.50).")
        return

    status = None
    if len(parts) > 1:
        status_str = parts[1].upper()
        if status_str in ["GREEN", "YELLOW", "RED", "GRAY"]:
            status = AvailabilityStatus[status_str]

    data = await state.get_data()
    station_id = data.get("report_station_id")
    if not station_id:
        await message.answer("Ошибка: АЗС не найдена. Попробуйте заново.")
        await state.clear()
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала /start")
            await state.clear()
            return
        station = await get_station_by_id(db, station_id)
        if not station:
            await message.answer("АЗС не найдена.")
            await state.clear()
            return

        fuel_type = FuelType.AI_95  # можно расширить, передавая fuel_type в состоянии
        await save_price(
            db,
            station_id=station_id,
            fuel_type=fuel_type,
            price=price,
            source=SourceType.USER,
            confidence=0.6
        )
        if status:
            await save_availability_report_with_consensus(
                db, station_id, fuel_type, status, SourceType.USER, confidence=0.6, user_id=user.id
            )
        user.reputation += 1
        await db.commit()

    from handlers.profile import get_user_level
    level_name, _, rep_to_next, _ = get_user_level(user.reputation)
    await message.answer(
        f"✅ Спасибо! Ваша цена сохранена. Вы получили +1 репутацию.\n"
        f"Текущий уровень: {level_name} (до следующего уровня осталось {rep_to_next} баллов).",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

@router.callback_query(F.data == "cancel_report")
async def cancel_report(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Отмена. Главное меню:", reply_markup=main_menu_keyboard())

# ======================================================================
# ГРАФИК ЦЕН (PRO) – с поддержкой топлива
# ======================================================================
@router.callback_query(lambda c: c.data.startswith("graph_"))
async def show_graph(callback: types.CallbackQuery):
    logger.info(f"[CALLBACK] graph_ вызван: {callback.data}")
    await callback.answer()
    parts = callback.data.split("_")
    if len(parts) < 2:
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return
    try:
        station_id = int(parts[1])
        fuel_code = "_".join(parts[2:]) if len(parts) > 2 else "AI-95"
        fuel_type = FuelType(fuel_code)
    except (IndexError, ValueError, KeyError) as e:
        logger.error(f"Ошибка парсинга: {e}")
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return
    fuel_type_str = fuel_type.value

    if not await check_pro(callback.from_user.id):
        await callback.message.answer(
            "⛔ <b>График цен доступен только для PRO-подписчиков</b>\n\n"
            "💎 Оформите PRO за 99 ₽/месяц и получите:\n"
            "• Уведомления о снижении цены\n"
            "• Оповещения о появлении топлива\n"
            "• Графики цен на АЗС\n\n"
            "Нажмите на кнопку «💎 PRO» в главном меню, чтобы оплатить.",
            reply_markup=pro_purchase_keyboard()
        )
        return
    logger.info(f"Генерируем график для station_id={station_id}, топливо={fuel_type_str}")
    try:
        graph_bytes = await generate_price_graph(station_id, fuel_type, days=30)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_to_menu")]
        ])
        if graph_bytes:
            await callback.message.answer_photo(
                photo=BufferedInputFile(graph_bytes, filename="price.png"),
                caption=f"📊 Динамика цены {fuel_type_str} за 30 дней",
                reply_markup=kb
            )
            logger.info("График отправлен с кнопкой меню")
        else:
            await callback.message.answer(
                f"📊 Недостаточно данных для построения графика для {fuel_type_str}.\n"
                "Для этой АЗС пока нет истории цен за 30 дней.\n"
                "Попробуйте позже, когда накопится больше данных.",
                reply_markup=kb
            )
            logger.info("Нет данных для графика")
    except Exception as e:
        logger.error(f"Ошибка при генерации графика: {e}", exc_info=True)
        await callback.message.answer(
            f"❌ Ошибка при генерации графика: {e}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_to_menu")]
            ])
        )

# ---------- Поделиться ----------
@router.callback_query(lambda c: c.data.startswith("share_"))
async def share_station(callback: types.CallbackQuery):
    station_id = int(callback.data.split("_")[1])
    async with AsyncSessionLocal() as db:
        station = await get_station_by_id(db, station_id)
        if not station:
            await callback.answer("АЗС не найдена")
            return
        fuel_type = FuelType.AI_95
        price_record = await get_latest_price(db, station_id, fuel_type)
        price = price_record.price if price_record else "неизвестна"
        status_record = await get_latest_availability(db, station_id, fuel_type)
        status = status_record.status.value if status_record else "неизвестно"
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.answer("Сначала /start")
            return

        ref_link = await get_referral_link(db, user)
        share_text = "Экономлю с BinzoLife — присоединяйся!"

        img = await generate_share_image(station.name, price, status, station.address, ref_link)
        if img:
            await callback.message.answer_photo(
                photo=BufferedInputFile(img, filename="share.png"),
                caption=f"📤 Поделитесь этой АЗС с друзьями!\n\n{share_text}\n\nВаша реферальная ссылка: {ref_link}"
            )
        else:
            await callback.message.answer(
                f"📤 Поделитесь этой АЗС с друзьями!\n\n{share_text}\n\nВаша реферальная ссылка: {ref_link}"
            )

        today = date.today()
        share_today = await db.execute(
            select(UserAction)
            .where(
                UserAction.user_id == user.id,
                UserAction.action == "share",
                func.date(UserAction.recorded_at) == today
            )
        )
        if not share_today.scalar_one_or_none():
            user.reputation += 1
            action = UserAction(user_id=user.id, action="share", station_id=station_id)
            db.add(action)
            await db.commit()
            await callback.message.answer("✅ Вы поделились! +1 репутация (можно 1 раз в день).\n\nА если ваш друг перейдёт по ссылке и сделает первый поиск — вы получите 3 дня PRO бесплатно.")
        else:
            await callback.message.answer("ℹ️ Вы уже получали репутацию за репост сегодня.")
