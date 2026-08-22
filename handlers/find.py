from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from sqlalchemy import select, func
from datetime import datetime, timedelta, timezone
from database.session import AsyncSessionLocal
from database.crud import (
    get_user, log_action, get_city_by_id, get_station_by_id,
    get_latest_price, create_notification, save_price,
    get_latest_fresh_price, get_latest_fresh_availability,
    save_availability_report_with_consensus
)
from database.models import FuelType, AvailabilityStatus, SourceType, Station, FuelPrice, AvailabilityReport
from services.rating import calculate_rating
from services.subscription import check_pro
from services.graphics import generate_price_graph
from utils.helpers import status_emoji, format_time_ago
from keyboards.reply import main_menu_keyboard, fuel_choice_keyboard
from keyboards.inline import station_action_keyboard, pro_purchase_keyboard, notification_action_keyboard

router = Router()

class FindStates(StatesGroup):
    choosing_fuel = State()

class ReportPriceStates(StatesGroup):
    waiting_price = State()

@router.message(F.text == "⛽ Найти заправку")
async def start_find(message: types.Message, state: FSMContext):
    await state.set_state(FindStates.choosing_fuel)
    await message.answer("Выберите вид топлива:", reply_markup=fuel_choice_keyboard())

@router.message(FindStates.choosing_fuel, F.text == "⛽ АИ-95")
async def choose_fuel(message: types.Message, state: FSMContext):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала выполните /start")
            await state.clear()
            return

        city_id = user.city_id
        if not city_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 Перейти в профиль", callback_data="go_profile")]
            ])
            await message.answer(
                "❌ Город не выбран.\nПожалуйста, установите город в профиле: 👤 Профиль → Изменить город",
                reply_markup=kb
            )
            await state.clear()
            return

        city = await get_city_by_id(db, city_id)
        if not city or city.latitude is None or city.longitude is None:
            await message.answer(
                "❌ У выбранного города не заданы координаты.\nОбратитесь к администратору.",
                reply_markup=main_menu_keyboard()
            )
            await state.clear()
            return

        lat, lon = city.latitude, city.longitude
        fuel_type = FuelType.AI_95

        # ---- Оптимизированный запрос ----
        station_ids_query = select(Station.id).where(Station.city_id == city_id, Station.is_active == True)
        station_ids = (await db.execute(station_ids_query)).scalars().all()
        if not station_ids:
            await message.answer("В этом городе пока нет АЗС.", reply_markup=main_menu_keyboard())
            await state.clear()
            return

        # Последние свежие цены
        price_subq = (
            select(FuelPrice.station_id, FuelPrice.fuel_type, func.max(FuelPrice.recorded_at).label("max_date"))
            .where(FuelPrice.station_id.in_(station_ids), FuelPrice.fuel_type == fuel_type, FuelPrice.is_fresh == True)
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

        # Последние свежие отчёты о наличии
        avail_subq = (
            select(AvailabilityReport.station_id, AvailabilityReport.fuel_type, func.max(AvailabilityReport.recorded_at).label("max_date"))
            .where(AvailabilityReport.station_id.in_(station_ids), AvailabilityReport.fuel_type == fuel_type, AvailabilityReport.is_fresh == True)
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

        # Средние цены за 30 дней
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        avg_stmt = select(func.avg(FuelPrice.price)).where(
            FuelPrice.station_id.in_(station_ids),
            FuelPrice.fuel_type == fuel_type,
            FuelPrice.recorded_at >= cutoff
        )
        min_stmt = select(func.min(FuelPrice.price)).where(
            FuelPrice.station_id.in_(station_ids),
            FuelPrice.fuel_type == fuel_type,
            FuelPrice.recorded_at >= cutoff
        )
        max_stmt = select(func.max(FuelPrice.price)).where(
            FuelPrice.station_id.in_(station_ids),
            FuelPrice.fuel_type == fuel_type,
            FuelPrice.recorded_at >= cutoff
        )
        avg_price = (await db.execute(avg_stmt)).scalar() or 0
        min_price = (await db.execute(min_stmt)).scalar() or 0
        max_price = (await db.execute(max_stmt)).scalar() or 0

        # Формируем результаты
        stations = (await db.execute(select(Station).where(Station.id.in_(station_ids)))).scalars().all()
        results = []
        for station in stations:
            price_rec = prices.get(station.id)
            if not price_rec:
                continue
            avail_rec = avails.get(station.id)
            rating_data = calculate_rating(
                station=station,
                user_lat=lat,
                user_lon=lon,
                price_record=price_rec,
                availability_record=avail_rec,
                avg_price_30d=avg_price or price_rec.price,
                min_price_30d=min_price or price_rec.price,
                max_price_30d=max_price or price_rec.price
            )
            results.append((rating_data["rating"], station, rating_data))

        results.sort(key=lambda x: x[0], reverse=True)
        top3 = results[:3]

        if not top3:
            await message.answer("Не найдено АЗС с актуальными ценами. Попробуйте позже.")
            await state.clear()
            return

        for rating, station, data in top3:
            status_text = status_emoji(data["availability"].value if data["availability"] else "GRAY")
            status_time = format_time_ago(data["availability_time"]) if data["availability_time"] else "неизвестно"
            price_time = format_time_ago(data["price_time"])
            text = (
                f"🏆 Рейтинг: {data['rating']}/100\n"
                f"⛽ {station.name}\n"
                f"📍 {station.address}\n"
                f"💰 {data['price']:.2f} ₽ (обновлено {price_time})\n"
                f"{status_text} Наличие: {data['availability'].value if data['availability'] else 'GRAY'} ({status_time})\n"
                f"📍 {data['distance_km']} км (по прямой) | ~{data['drive_time_min']} мин\n"
                f"📌 {data['explanation']}\n"
            )
            await message.answer(
                text,
                reply_markup=station_action_keyboard(
                    station.id,
                    data["price"],
                    data["availability"],
                    station.latitude,
                    station.longitude,
                    city_id
                )
            )
            await log_action(db, user.id, "search_result", station.id)

        await message.answer("Выберите заправку для деталей или вернитесь в меню.", reply_markup=main_menu_keyboard())
        await state.clear()

# ---------- Переход в профиль ----------
@router.callback_query(F.data == "go_profile")
async def go_profile_callback(callback: types.CallbackQuery):
    await callback.answer()
    from handlers.profile import show_profile
    await show_profile(callback.message)

# ---------- Следить за ценой (PRO) ----------
@router.callback_query(lambda c: c.data.startswith("follow_"))
async def follow_price(callback: types.CallbackQuery):
    await callback.answer()
    station_id = int(callback.data.split("_")[1])
    
    if not await check_pro(callback.from_user.id):
        await callback.message.answer(
            "🔔 Уведомления доступны только в PRO.\n"
            "Купите PRO за 99 ₽/месяц и получайте оповещения о снижении цен.",
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
        
        latest_price = await get_latest_fresh_price(db, station_id, FuelType.AI_95)
        if not latest_price:
            await callback.message.answer("Не удалось получить текущую цену.")
            return
        
        target_price = round(latest_price.price - 0.5, 2)
        
        await create_notification(
            db,
            user_id=user.id,
            fuel_type=FuelType.AI_95,
            station_id=station_id,
            target_price=target_price,
            notify_on_low_price=True
        )
        await callback.message.answer(
            f"✅ Подписка на цену на АЗС <b>{station.name}</b> активирована.\n"
            f"Я сообщу, когда цена станет ≤ {target_price} ₽.\n"
            f"(Вы можете отписаться в разделе «Мои уведомления».)"
        )

# ---------- Уведомления о появлении (PRO) ----------
@router.callback_query(lambda c: c.data.startswith("alert_avail_"))
async def subscribe_availability(callback: types.CallbackQuery):
    await callback.answer()
    station_id = int(callback.data.split("_")[2])
    if not await check_pro(callback.from_user.id):
        await callback.answer("Доступно только в PRO", show_alert=True)
        return
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.answer("Сначала /start")
            return
        station = await get_station_by_id(db, station_id)
        if not station:
            await callback.answer("АЗС не найдена")
            return
        await create_notification(
            db,
            user_id=user.id,
            fuel_type=FuelType.AI_95,
            station_id=station_id,
            notify_on_availability=True
        )
    await callback.answer("Вы подписаны на уведомления о появлении топлива на этой АЗС")
    await callback.message.answer(f"🔔 Вы будете получать уведомления, когда на {station.name} появится АИ-95.")

# ---------- Сообщить цену (краудсорсинг) ----------
@router.callback_query(lambda c: c.data.startswith("report_price_"))
async def start_report_price(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    station_id = int(callback.data.split("_")[2])
    await state.update_data(station_id=station_id)
    await state.set_state(ReportPriceStates.waiting_price)
    await callback.message.answer(
        "Введите актуальную цену на АИ‑95 на этой АЗС (в рублях, например, 68.50):\n"
        "Или нажмите «Отмена», чтобы не сообщать.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
        ])
    )

@router.message(ReportPriceStates.waiting_price, F.text)
async def process_report_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text.replace(',', '.'))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Пожалуйста, введите положительное число (например, 68.50).")
        return

    data = await state.get_data()
    station_id = data.get("station_id")
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

        # сохраняем цену
        await save_price(
            db,
            station_id=station_id,
            fuel_type=FuelType.AI_95,
            price=price,
            source=SourceType.USER,
            confidence=0.6
        )
        # обновляем репутацию
        user.reputation += 1
        await db.commit()

    await message.answer(
        f"✅ Спасибо! Цена для {station.name} обновлена до {price:.2f} ₽.\n"
        f"Ваша репутация +1 (всего {user.reputation}).",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

@router.callback_query(F.data == "cancel_report")
async def cancel_report(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Отмена. Главное меню:", reply_markup=main_menu_keyboard())

# ---------- График цен (PRO) ----------
@router.callback_query(lambda c: c.data.startswith("graph_"))
async def show_graph(callback: types.CallbackQuery):
    await callback.answer()
    if not await check_pro(callback.from_user.id):
        await callback.answer("Доступно только в PRO", show_alert=True)
        return
    station_id = int(callback.data.split("_")[1])
    graph_bytes = await generate_price_graph(station_id, FuelType.AI_95, days=30)
    if graph_bytes:
        await callback.message.answer_photo(
            photo=BufferedInputFile(graph_bytes, filename="price.png"),
            caption=f"📊 Динамика цены за 30 дней"
        )
    else:
        await callback.message.answer("Недостаточно данных для графика.")
