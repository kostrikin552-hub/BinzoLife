from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from database.session import AsyncSessionLocal
from database.crud import get_user, find_nearest_green_station, get_city_by_id, get_latest_fresh_price
from database.models import FuelType
from utils.helpers import haversine_distance
from keyboards.reply import main_menu_keyboard

router = Router()

@router.message(F.text == "🚨 Бензин заканчивается!")
async def emergency_search(message: types.Message):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user or not user.city_id:
            await message.answer("Сначала установите город в профиле.")
            return
        city = await get_city_by_id(db, user.city_id)
        if not city or city.latitude is None:
            await message.answer("У вашего города не заданы координаты. Обратитесь к администратору.")
            return
        station = await find_nearest_green_station(db, city.id, city.latitude, city.longitude, radius_km=5.0)
        if not station:
            await message.answer(
                "🚨 К сожалению, в радиусе 5 км нет АЗС с подтверждённым наличием АИ-95.\n"
                "Попробуйте обычный поиск или расширьте радиус (временно недоступно).",
                reply_markup=main_menu_keyboard()
            )
            return
        price = await get_latest_fresh_price(db, station.id, FuelType.AI_95)
        price_text = f"{price.price:.2f} ₽" if price else "неизвестна"
        dist = haversine_distance(city.latitude, city.longitude, station.latitude, station.longitude)
        await message.answer(
            f"🚨 <b>ТРЕВОЖНЫЙ ПОИСК!</b>\n\n"
            f"Ближайшая АЗС с наличием АИ-95:\n"
            f"⛽ {station.name}\n"
            f"📍 {station.address}\n"
            f"💰 {price_text}\n"
            f"📏 {dist:.1f} км от вас\n"
            f"🚗 <a href='https://yandex.ru/maps/?pt={station.longitude},{station.latitude}&z=15'>Построить маршрут</a>",
            reply_markup=main_menu_keyboard()
        )
