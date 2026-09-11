# handlers/find.py — ПОЛНАЯ ФИНАЛЬНАЯ ВЕРСИЯ
import html
import logging
import io
import asyncio
import qrcode
from datetime import datetime, timedelta, timezone, date
from PIL import Image, ImageDraw, ImageFont
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, CallbackQuery
from sqlalchemy import select, func, update, text

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_user, log_action, get_city_by_id, get_station_by_id,
    get_latest_price, get_latest_availability, create_notification, save_price,
    get_latest_fresh_price, get_latest_fresh_availability,
    get_avg_price_30d, get_min_price_30d, get_max_price_30d,
    set_first_search, get_active_notifications_for_user,
    activate_trial, increment_station_views, get_referral_link,
    save_availability_report_with_consensus,
    get_cached_address, cache_address,
    can_use_free_search, use_free_search,
    get_stations_in_radius, get_user_by_id, add_free_pro_days,
    set_user_timezone, save_user_location, find_nearest_city,
    commit_or_rollback, get_stations_by_city
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


def parse_fuel_type_from_callback(val: str) -> FuelType:
    if not val:
        return FuelType.AI_95
    val_clean = val.strip().upper().replace("-", "_")
    for ft in FuelType:
        if ft.name == val_clean or ft.value == val:
            return ft
    return FuelType.AI_95


def calculate_true_savings(
    station_price: float,
    avg_city_price: float,
    distance_km: float,
    tank_volume: float = 50,
    consumption_per_100km: float = 9
) -> dict:
    if avg_city_price is None or avg_city_price <= 0 or station_price <= 0:
        return {"gross_savings": 0, "trip_cost": 0, "net_savings": 0, "is_worth": False, "badge": "Цены не подтверждены"}

    if consumption_per_100km <= 0:
        price_diff_per_liter = avg_city_price - station_price
        if price_diff_per_liter < 0:
            overpay = abs(price_diff_per_liter) * tank_volume
            return {"gross_savings": 0, "trip_cost": 0, "net_savings": -overpay, "is_worth": False,
                    "badge": f"⚠️ Цена выше средней: переплата ~{overpay:.0f} ₽ на бак"}
        gross_savings = price_diff_per_liter * tank_volume
        return {"gross_savings": round(gross_savings, 1), "trip_cost": 0, "net_savings": round(gross_savings, 1),
                "is_worth": gross_savings > 10.0, "badge": f"✅ Экономия: +{gross_savings:.0f} ₽ на баке"}

    price_diff_per_liter = avg_city_price - station_price
    if price_diff_per_liter < 0:
        overpay = abs(price_diff_per_liter) * tank_volume
        return {"gross_savings": 0, "trip_cost": 0, "net_savings": -overpay, "is_worth": False,
                "badge": f"⚠️ Цена выше средней: переплата ~{overpay:.0f} ₽ на бак"}

    gross_savings = price_diff_per_liter * tank_volume
    round_trip_km = distance_km * 2.0
    fuel_spent_liters = (round_trip_km / 100.0) * consumption_per_100km
    travel_cost = fuel_spent_liters * station_price
    net_savings = round(gross_savings - travel_cost, 1)
    is_worth = net_savings > 10.0

    if is_worth:
        badge = f"✅ Чистый профит: +{net_savings:.0f} ₽ (с учётом дороги)"
    else:
        badge = f"⚠️ Дорога съест выгоду: расход на поездку {travel_cost:.0f} ₽"
    return {"gross_savings": round(gross_savings, 1), "trip_cost": round(travel_cost, 1),
            "net_savings": net_savings, "is_worth": is_worth, "badge": badge}


async def get_fuel_status(db, station_id: int, fuel_type: FuelType) -> dict:
    result = await db.execute(
        text("""
            SELECT price, availability, queue_level, observed_at, source
            FROM station_current_fuel
            WHERE station_id = :station_id AND fuel_type = :fuel_type
            ORDER BY observed_at DESC LIMIT 1
        """),
        {"station_id": station_id, "fuel_type": fuel_type.value}
    )
    row = result.fetchone()
    if row and row.availability != "unknown":
        return {"price": row.price, "availability": row.availability, "queue_level": row.queue_level,
                "observed_at": row.observed_at, "source": row.source or "Радар MultiGo"}

    avail_rec = await get_latest_fresh_availability(db, station_id, fuel_type)
    price_rec = await get_latest_fresh_price(db, station_id, fuel_type)
    if not price_rec:
        price_rec = await get_latest_price(db, station_id, fuel_type)

    avail_val = "available" if (avail_rec and avail_rec.status == AvailabilityStatus.GREEN) else "unknown"
    if avail_rec and avail_rec.status == AvailabilityStatus.RED:
        avail_val = "unavailable"
    elif avail_rec and avail_rec.status == AvailabilityStatus.YELLOW:
        avail_val = "limited"

    source_val = "Сеть АЗС"
    if price_rec and price_rec.source:
        if price_rec.source == SourceType.USER:
            source_val = "Водители"
        elif price_rec.source == SourceType.PARSER:
            source_val = "Мониторинг АЗС"

    return {"price": price_rec.price if price_rec else None, "availability": avail_val,
            "queue_level": "low" if avail_val == "available" else "unknown",
            "observed_at": price_rec.recorded_at if price_rec else None, "source": source_val}


def _sync_generate_share_image(name: str, price: float, status: str, address: str, ref_link: str) -> bytes:
    try:
        img = Image.new('RGB', (800, 400), color='#1a1a2e')
        draw = ImageDraw.Draw(img)
        try:
            font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
            font_text = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        except Exception:
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


async def generate_share_image(name: str, price: float, status: str, address: str, ref_link: str) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_generate_share_image, name, price, status, address, ref_link)


@router.message(F.text == "⛽ Найти заправку")
async def start_find(message: types.Message, state: FSMContext):
    await state.set_state(FindStates.choosing_fuel)
    await message.answer("Выберите вид топлива:", reply_markup=fuel_choice_keyboard())


@router.message(FindStates.choosing_fuel, F.text.in_(["⛽ АИ-92", "⛽ АИ-95", "⛽ АИ-98", "⛽ АИ-100", "⛽ ДТ"]))
async def choose_fuel(message: types.Message, state: FSMContext):
    fuel_map = {
        "⛽ АИ-92": FuelType.AI_92, "⛽ АИ-95": FuelType.AI_95,
        "⛽ АИ-98": FuelType.AI_98, "⛽ АИ-100": FuelType.AI_100, "⛽ ДТ": FuelType.DT,
    }
    fuel_type = fuel_map.get(message.text)
    if not fuel_type:
        await message.answer("Пожалуйста, выберите топливо из списка.")
        return

    await state.update_data(fuel_type=fuel_type)

    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            user = await create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)

        if not user.city_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 Перейти в профиль", callback_data="go_profile")]
            ])
            await message.answer("❌ Город не выбран. Пожалуйста, установите город в профиле.", reply_markup=kb)
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
        "Как отсортировать результаты?\n\n🔥 По рейтингу (баланс цены и наличия)\n💰 По минимальной цене",
        reply_markup=sort_choice_keyboard()
    )


@router.message(FindStates.choosing_fuel, F.text == "◀️ Назад")
async def back_to_menu_from_fuel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())


@router.message(FindStates.choosing_fuel)
async def handle_unknown_in_choosing_fuel(message: types.Message, state: FSMContext):
    await message.answer("Пожалуйста, воспользуйтесь кнопками ниже.", reply_markup=fuel_choice_keyboard())


@router.message(FindStates.choosing_sort, F.text)
async def handle_text_in_sort_state(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("⏳ Поиск был сброшен. Воспользуйтесь меню:", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "sort_rating")
async def sort_rating(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(sort_mode="rating")
    await perform_search(callback.message, state, user_telegram_id=callback.from_user.id)


@router.callback_query(F.data == "sort_price")
async def sort_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(sort_mode="price")
    await perform_search(callback.message, state, user_telegram_id=callback.from_user.id)


async def perform_search(message: types.Message, state: FSMContext, user_telegram_id: int = None):
    try:
        target_user_id = user_telegram_id if user_telegram_id else message.from_user.id
        data = await state.get_data()
        fuel_type = data.get("fuel_type", FuelType.AI_95)
        sort_mode = data.get("sort_mode", "rating")

        async with AsyncSessionLocal() as db:
            user = await get_user(db, target_user_id)
            if not user:
                user = await create_user(db, target_user_id, None, None)

            if not user.city_id:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👤 Выбрать город", callback_data="go_profile")]
                ])
                await message.answer("❌ Город не выбран. Пожалуйста, укажите город в профиле.", reply_markup=kb)
                await state.clear()
                return

            city = await get_city_by_id(db, user.city_id)
            if not city or city.latitude is None or city.longitude is None:
                await message.answer(
                    "❌ У выбранного города не заданы координаты. Выберите другой город в профиле.",
                    reply_markup=main_menu_keyboard()
                )
                await state.clear()
                return

            search_lat = city.latitude
            search_lon = city.longitude
            if user.last_lat and user.last_lon:
                dist_to_city = haversine_distance(user.last_lat, user.last_lon, city.latitude, city.longitude)
                if dist_to_city <= 80.0:
                    search_lat = user.last_lat
                    search_lon = user.last_lon

            is_pro = await check_pro(user.telegram_id)
            if not is_pro:
                can_search = await can_use_free_search(db, user.id)
                if not can_search:
                    await message.answer(
                        "🛑 Бесплатный поиск на сегодня завершён.\n\n"
                        "👑 Подключи **PRO** и получи безлимитный поиск с расчётом чистой выгоды.\n\n"
                        "💡 **99 ₽/мес** — окупается с первой заправки!",
                        reply_markup=pro_purchase_keyboard()
                    )
                    await state.clear()
                    return
                await use_free_search(db, user.id)

            stations = await get_stations_in_radius(db, city.id, search_lat, search_lon, radius_km=35.0)
            is_fallback = False
            if not stations:
                stations = await get_stations_by_city(db, city.id)
                is_fallback = True
                if not stations:
                    await message.answer(
                        f"⛽ В г. <b>{html.escape(city.name)}</b> пока нет станций в базе.",
                        parse_mode="HTML",
                        reply_markup=main_menu_keyboard()
                    )
                    await state.clear()
                    return

            station_ids = [s.id for s in stations]

            price_stmt = (
                select(FuelPrice)
                .where(FuelPrice.station_id.in_(station_ids), FuelPrice.fuel_type == fuel_type)
                .order_by(FuelPrice.recorded_at.desc())
            )
            price_res = await db.execute(price_stmt)
            prices = {}
            for p in price_res.scalars().all():
                if p.station_id not in prices:
                    prices[p.station_id] = p

            avg_price = await get_avg_price_30d(db, city.id, fuel_type) or 56.0
            min_price = await get_min_price_30d(db, city.id, fuel_type) or 52.0
            max_price = await get_max_price_30d(db, city.id, fuel_type) or 60.0

            results = []
            for station in stations:
                price_rec = prices.get(station.id)
                if not price_rec:
                    results.append({
                        "station": station, "price": None, "price_time": None,
                        "availability": AvailabilityStatus.GRAY, "availability_time": None,
                        "distance_km": haversine_distance(search_lat, search_lon, station.latitude, station.longitude),
                        "rating": 0, "explanation": "Цена не подтверждена",
                        "price_diff": 0, "avg_price": avg_price, "no_price": True,
                    })
                    continue

                dist = haversine_distance(search_lat, search_lon, station.latitude, station.longitude)
                rating_data = calculate_rating(
                    station=station, price_record=price_rec, availability_record=None,
                    avg_price_30d=avg_price, min_price_30d=min_price, max_price_30d=max_price
                )
                results.append({
                    "station": station, "price": price_rec.price, "price_time": price_rec.recorded_at,
                    "availability": AvailabilityStatus.GREEN, "availability_time": price_rec.recorded_at,
                    "distance_km": dist, "rating": rating_data["rating"],
                    "explanation": rating_data["explanation"], "price_diff": rating_data.get("price_diff", 0),
                    "avg_price": avg_price, "no_price": False,
                })

            if not results:
                await message.answer("Не удалось получить котировки. Попробуйте сменить марку топлива.")
                await state.clear()
                return

            if sort_mode == "price":
                results.sort(key=lambda x: (x.get("no_price", False), x["price"] if x["price"] is not None else float('inf')))
            else:
                results.sort(key=lambda x: (x.get("no_price", False), -x["rating"]))

            if user and not user.has_made_first_search:
                await set_first_search(db, user.id)

            if is_fallback:
                await message.answer("⚠️ В радиусе 35 км заправок не найдено. Показываем доступные АЗС города.")

            await state.update_data(all_results=results, current_index=0, is_pro=is_pro)
            await show_station_card(message, results[0], 0, len(results), is_pro, state,
                                    fuel_type=fuel_type, user_telegram_id=target_user_id)
            await log_action(db, user.id, "search_result")

    except Exception as e:
        logger.error(f"Ошибка в perform_search: {e}", exc_info=True)
        await message.answer("⚠️ Произошла ошибка при поиске. Попробуйте снова.")
        await state.clear()


async def show_station_card(
    message: types.Message,
    result: dict,
    index: int,
    total: int,
    is_pro: bool,
    state: FSMContext,
    fuel_type: FuelType = None,
    user_telegram_id: int = None
):
    try:
        if not result:
            await message.answer("Ошибка: нет данных.")
            return
        station = result.get("station")
        if not station:
            await message.answer("Ошибка: АЗС не найдена.")
            return

        target_user_id = user_telegram_id if user_telegram_id else message.from_user.id

        price = result.get("price", 0.0)
        price_time = result.get("price_time")
        availability = result.get("availability", AvailabilityStatus.GRAY)
        distance_km = result.get("distance_km", 0.0)
        rating = result.get("rating", 0)
        avg_price = result.get("avg_price", 0)
        no_price = result.get("no_price", False)

        station_name = html.escape(station.name)
        fuel_type_str = fuel_type.value if fuel_type else "АИ-95"
        rank_badges = {1: "🥇 Лучший выбор", 2: "🥈 Вариант №2", 3: "🥉 Вариант №3"}
        badge = rank_badges.get(index + 1, f"АЗС #{index + 1}")

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

        async with AsyncSessionLocal() as db:
            fuel_status = await get_fuel_status(db, station.id, fuel_type)
            if fuel_status.get("price") and fuel_status["price"] > 0:
                price = fuel_status["price"]
                price_time = fuel_status.get("observed_at")

            availability_emoji = {"available": "🟢", "limited": "🟡", "unavailable": "🔴", "unknown": "⚪"}
            availability_text = {"available": "Есть", "limited": "Осталось мало", "unavailable": "Нет", "unknown": "неизвестно"}
            queue_text = {"low": "🟢 Свободно (до 3 машин)", "medium": "🟡 Небольшая очередь (4–8 машин)",
                          "high": "🔴 Большая очередь (9+ машин)", "unknown": "⚪ нет данных"}

            avail_emoji = availability_emoji.get(fuel_status.get("availability", "unknown"), "⚪")
            avail_text = availability_text.get(fuel_status.get("availability", "unknown"), "неизвестно")
            queue_str = queue_text.get(fuel_status.get("queue_level", "unknown"), "⚪ нет данных")
            status_observed_str = format_time_ago(fuel_status.get("observed_at")) if fuel_status.get("observed_at") else "неизвестно"
            source_str = html.escape(fuel_status.get("source") or "Сеть АЗС")

            status_line = f"{avail_emoji} Наличие: {avail_text}"
            if fuel_status.get("observed_at"):
                status_line += f" (обновлено {status_observed_str})"
            status_line += f"\n⏳ Очередь: {queue_str}\n📡 Источник: {source_str}"

        price_text_formatted = "❌ Цена не подтверждена" if no_price else (f"{price:.2f} ₽" if price else "нет данных")
        rating_display = "❌" if no_price else f"⭐ {round(rating / 20, 1)} ({rating}/100)"

        async with AsyncSessionLocal() as db:
            user = await get_user(db, target_user_id)
            tank_volume = user.tank_volume if user else 50

        savings_data = {}
        if not no_price and avg_price and price and distance_km > 0:
            savings_data = calculate_true_savings(
                station_price=price, avg_city_price=avg_price,
                distance_km=distance_km, tank_volume=tank_volume
            )

        distance_text = f"{distance_km:.1f} км" if 0 < distance_km < 1000 else "расстояние неизвестно"
        time_text = f"\n🚦 В пути: ~{round(distance_km / 40 * 60)} мин" if distance_km > 0 else ""

        text = (
            f"<b>{badge}</b> • <b>{station_name}</b> {rating_display}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⛽ <b>{fuel_type_str}:</b> <b>{price_text_formatted}</b>\n"
            f"📍 <code>{station_address}</code> (~{distance_text}){time_text}\n"
            f"{status_line}"
        )

        if not no_price and savings_data.get("badge"):
            text += f"\n\n💡 {savings_data['badge']}"

        if not is_pro:
            async with AsyncSessionLocal() as db:
                user = await get_user(db, target_user_id)
                if user:
                    today = date.today()
                    remaining = user.free_searches_today if user.last_free_search_date == today else 1
                    text += f"\n\n🔍 Осталось бесплатных поисков: {remaining} из 1." if remaining > 0 else "\n\n🔍 Бесплатный поиск на сегодня использован."
            text += "\n—\n⚠️ Оформите PRO для безлимитного поиска."

        keyboard = station_action_keyboard(
            station_id=station.id, price=price if price is not None else 0.0,
            availability=availability, lat=station.latitude, lon=station.longitude,
            city_id=station.city_id, is_pro=is_pro, index=index, total=total, fuel_type=fuel_type_str
        )
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка при отправке карточки: {e}", exc_info=True)
        await message.answer("Произошла ошибка при отправке карточки.")


@router.callback_query(F.data.startswith("more_"))
async def show_more_stations(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        data = await state.get_data()
        all_results = data.get("all_results", [])
        current_index = data.get("current_index", 0)
        if not all_results or current_index >= len(all_results):
            await callback.answer("Больше заправок не найдено ⛽", show_alert=True)
            return

        next_index = current_index + 1
        more_results = all_results[next_index:next_index+2]
        if not more_results:
            await callback.answer("Больше заправок не найдено ⛽", show_alert=True)
            return

        text = "Вот ещё две АЗС с хорошими ценами:\n\n"
        for i, res in enumerate(more_results, start=next_index+1):
            station = res.get("station")
            if not station:
                continue
            price = res.get("price")
            dist = res.get("distance_km", 0.0)
            status = res.get("availability", AvailabilityStatus.GRAY).value
            map_link = f"<a href='https://yandex.ru/maps/?pt={station.longitude},{station.latitude}&z=15'>Маршрут</a>" if station.latitude else "Маршрут недоступен"
            price_text = f"{price:.2f} ₽" if price else "нет данных"
            text += f"{i}. <b>{html.escape(station.name)}</b> — {price_text}, {status}, {dist:.1f} км\n   🗺 {map_link}\n\n"

        new_index = next_index + len(more_results)
        await state.update_data(current_index=new_index)

        kb_buttons = []
        if new_index < len(all_results):
            kb_buttons.append([InlineKeyboardButton(text="🔍 Показать ещё 2", callback_data="more_next")])
        kb_buttons.append([InlineKeyboardButton(text="🔙 Назад к лучшей", callback_data="back_to_best")])
        await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons), parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в show_more: {e}", exc_info=True)


@router.callback_query(F.data == "back_to_best")
async def back_to_best(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        data = await state.get_data()
        all_results = data.get("all_results", [])
        is_pro = data.get("is_pro", False)
        fuel_type = data.get("fuel_type", FuelType.AI_95)
        if all_results:
            await show_station_card(
                callback.message, all_results[0], 0, len(all_results), is_pro, state,
                fuel_type=fuel_type, user_telegram_id=callback.from_user.id
            )
        else:
            await callback.message.answer("Нет сохранённых результатов.", reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"Ошибка в back_to_best: {e}", exc_info=True)


@router.callback_query(F.data == "restart_search")
async def restart_search(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await start_find(callback.message, state)


@router.callback_query(F.data == "go_profile")
async def go_profile_callback(callback: CallbackQuery):
    await callback.answer()
    from handlers.profile import show_profile
    await show_profile(callback.message, user_telegram_id=callback.from_user.id)


@router.callback_query(F.data.startswith("report_price_"))
async def start_report_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    parts = callback.data.split("_")
    station_id = int(parts[2])
    fuel_code = parts[3] if len(parts) > 3 else "AI-95"
    await state.update_data(report_station_id=station_id, report_fuel_type=fuel_code)
    await state.set_state(ReportPriceStates.waiting_price)
    await callback.message.answer(
        f"✏️ Введите актуальную цену для {fuel_code} (в рублях, например 68.50):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]])
    )


@router.message(ReportPriceStates.waiting_price, F.text)
async def process_report_price(message: types.Message, state: FSMContext):
    if message.text in ["◀️ Назад", "/start", "/help", "👤 Профиль", "⛽ Найти заправку"]:
        await state.clear()
        await message.answer("Ввод отменён.")
        return

    parts = message.text.strip().split()
    try:
        price = float(parts[0].replace(',', '.'))
        if not (35.0 <= price <= 150.0):
            raise ValueError
    except ValueError:
        await message.answer("❌ Введите корректную цену от 35 до 150 ₽.")
        return

    data = await state.get_data()
    station_id = data.get("report_station_id")
    fuel_code = data.get("report_fuel_type", "AI-95")

    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала выполните /start")
            await state.clear()
            return

        fuel_type = parse_fuel_type_from_callback(fuel_code)
        await save_price(db, station_id=station_id, fuel_type=fuel_type, price=price, source=SourceType.USER, confidence=0.6, user_id=user.id)
        user.reputation += 1
        await commit_or_rollback(db)

    await state.clear()
    await message.answer("🎉 Спасибо! Цена обновлена. Вам начислен +1 балл репутации.", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "cancel_report")
async def cancel_report(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Отмена.", reply_markup=main_menu_keyboard())


@router.callback_query(F.data.startswith("follow_"))
async def follow_price(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    parts = callback.data.split("_")
    station_id = int(parts[1])
    fuel_code = "_".join(parts[2:]) if len(parts) > 2 else "AI-95"
    fuel_type = parse_fuel_type_from_callback(fuel_code)

    if not await check_pro(callback.from_user.id):
        await callback.message.answer("⛔ Уведомления доступны только в PRO-подписке.", reply_markup=pro_purchase_keyboard())
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        latest_price = await get_latest_fresh_price(db, station_id, fuel_type)
        if not latest_price:
            await callback.message.answer(f"Не удалось получить цену для {fuel_type.value}.")
            return
        target = max(0.0, round(latest_price.price - 0.5, 2))
        await create_notification(db, user_id=user.id, fuel_type=fuel_type, station_id=station_id, target_price=target, notify_on_low_price=True)

    await callback.message.answer(f"✅ Вы подписаны на уведомление при цене ≤ {target} ₽.")


@router.callback_query(F.data.startswith("alert_avail_"))
async def subscribe_availability(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    parts = callback.data.split("_")
    station_id = int(parts[2])
    fuel_code = "_".join(parts[3:]) if len(parts) > 3 else "AI-95"
    fuel_type = parse_fuel_type_from_callback(fuel_code)

    if not await check_pro(callback.from_user.id):
        await callback.message.answer("⛔ Уведомления доступны только в PRO.", reply_markup=pro_purchase_keyboard())
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        await create_notification(db, user_id=user.id, fuel_type=fuel_type, station_id=station_id, notify_on_availability=True)

    await callback.message.answer(f"🔔 Вы получите уведомление, когда появится {fuel_type.value}.")


@router.callback_query(F.data.startswith("graph_"))
async def show_graph(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    parts = callback.data.split("_")
    station_id = int(parts[1])
    fuel_code = "_".join(parts[2:]) if len(parts) > 2 else "AI-95"
    fuel_type = parse_fuel_type_from_callback(fuel_code)

    if not await check_pro(callback.from_user.id):
        await callback.message.answer("⛔ График цен доступен только для PRO-подписчиков.", reply_markup=pro_purchase_keyboard())
        return

    graph_bytes = await generate_price_graph(station_id, fuel_type, days=30)
    if graph_bytes:
        await callback.message.answer_photo(
            photo=BufferedInputFile(graph_bytes, filename="price.png"),
            caption=f"📊 Динамика цены {fuel_type.value} за 30 дней",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Меню", callback_data="back_to_menu")]])
        )
    else:
        await callback.message.answer("📊 Недостаточно данных для построения графика.")


@router.callback_query(F.data.startswith("share_"))
async def share_station(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fuel_type = data.get("fuel_type", FuelType.AI_95)
    station_id = int(callback.data.split("_")[1])

    async with AsyncSessionLocal() as db:
        station = await get_station_by_id(db, station_id)
        if not station:
            await callback.answer("АЗС не найдена.")
            return
        user = await get_user(db, callback.from_user.id)
        ref_link = await get_referral_link(db, user)
        img = await generate_share_image(station.name, 0.0, "Есть", station.address or "", ref_link)

        if img:
            await callback.message.answer_photo(
                photo=BufferedInputFile(img, filename="share.png"),
                caption=f"📤 Поделитесь ссылкой: {ref_link}"
            )
        else:
            await callback.message.answer(f"📤 Ссылка для друзей: {ref_link}")
