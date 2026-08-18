from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from database.session import AsyncSessionLocal
from database.crud import (
    get_user, get_stations_by_city, get_latest_price, get_latest_availability,
    log_action, get_avg_price_30d, get_min_price_30d, get_max_price_30d
)
from database.models import FuelType
from services.rating import calculate_rating
from utils.helpers import status_emoji, format_time_ago
from keyboards.reply import main_menu_keyboard, fuel_choice_keyboard, location_request_keyboard
from keyboards.inline import station_action_keyboard

router = Router()  # <-- ЭТО ОБЯЗАТЕЛЬНО!

class FindStates(StatesGroup):
    choosing_fuel = State()
    waiting_location = State()

@router.message(F.text == "⛽ Найти заправку")
async def start_find(message: types.Message, state: FSMContext):
    await state.set_state(FindStates.choosing_fuel)
    await message.answer("Выберите вид топлива:", reply_markup=fuel_choice_keyboard())

@router.message(FindStates.choosing_fuel, F.text == "⛽ АИ-95")
async def choose_fuel(message: types.Message, state: FSMContext):
    await state.update_data(fuel_type="AI-95")
    await state.set_state(FindStates.waiting_location)
    await message.answer(
        "📍 Отправьте вашу геолокацию или выберите город в профиле.",
        reply_markup=location_request_keyboard()
    )

@router.message(FindStates.waiting_location, F.location)
async def handle_location(message: types.Message, state: FSMContext):
    lat = message.location.latitude
    lon = message.location.longitude
    data = await state.get_data()
    fuel_type_str = data.get("fuel_type", "AI-95")
    try:
        fuel_type = FuelType(fuel_type_str)
    except ValueError:
        fuel_type = FuelType.AI_95

    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала выполните /start")
            await state.clear()
            return
        city_id = user.city_id
        if not city_id:
            await message.answer("Город не задан. Установите его в профиле (/profile)")
            await state.clear()
            return

        stations = await get_stations_by_city(db, city_id)
        if not stations:
            await message.answer("В этом городе пока нет АЗС в базе. Попробуйте позже.")
            await state.clear()
            return

        avg_price = await get_avg_price_30d(db, city_id, fuel_type) or 0
        min_price = await get_min_price_30d(db, city_id, fuel_type) or 0
        max_price = await get_max_price_30d(db, city_id, fuel_type) or 0

        results = []
        for st in stations:
            price = await get_latest_price(db, st.id, fuel_type)
            if not price:
                continue
            avail = await get_latest_availability(db, st.id, fuel_type)
            rating_data = calculate_rating(
                station=st,
                user_lat=lat,
                user_lon=lon,
                price_record=price,
                availability_record=avail,
                avg_price_30d=avg_price or price.price,
                min_price_30d=min_price or price.price,
                max_price_30d=max_price or price.price
            )
            results.append((rating_data["rating"], st, rating_data))

        results.sort(key=lambda x: x[0], reverse=True)
        top3 = results[:3]

        if not top3:
            await message.answer("Не найдено АЗС с актуальными ценами. Попробуйте позже.")
            await state.clear()
            return

        for rating, st, data in top3:
            status_text = status_emoji(data["availability"].value if data["availability"] else "GRAY")
            status_time = format_time_ago(data["availability_time"]) if data["availability_time"] else "неизвестно"
            price_time = format_time_ago(data["price_time"])
            text = (
                f"🏆 Рейтинг: {data['rating']}/100\n"
                f"⛽ {st.name}\n"
                f"💰 {data['price']:.2f} ₽ (обновлено {price_time})\n"
                f"{status_text} Наличие: {data['availability'].value if data['availability'] else 'GRAY'} ({status_time})\n"
                f"📍 {data['distance_km']} км (по прямой) | ~{data['drive_time_min']} мин\n"
                f"📌 {data['explanation']}\n"
            )
            await message.answer(
                text,
                reply_markup=station_action_keyboard(
                    st.id,
                    data["price"],
                    data["availability"],
                    st.latitude,
                    st.longitude
                )
            )
            await log_action(db, user.id, "search_result", st.id)

        await message.answer("Выберите заправку для деталей или вернитесь в меню.", reply_markup=main_menu_keyboard())
        await state.clear()

@router.message(FindStates.waiting_location, F.text == "◀️ Назад")
async def back_from_location(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())
