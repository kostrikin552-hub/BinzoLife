from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from database.session import AsyncSessionLocal
from database.crud import get_user, update_user, get_city_by_name, is_user_pro
from keyboards.reply import main_menu_keyboard, back_to_menu_keyboard
from services.subscription import format_pro_until
from datetime import datetime

router = Router()

class ProfileStates(StatesGroup):
    waiting_city = State()
    waiting_tank = State()

@router.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала выполните /start")
            return
        city_name = user.city.name if user.city else "Не задан"
        pro_active = await is_user_pro(db, user)
        if pro_active and user.pro_until:
            pro_status = f"✅ Активен до {format_pro_until(user.pro_until)}"
        else:
            pro_status = "❌ Не активен"
        text = (
            f"👤 Профиль\n"
            f"ID: {user.telegram_id}\n"
            f"Город: {city_name}\n"
            f"Топливо по умолчанию: {user.default_fuel}\n"
            f"Объём бака: {user.tank_volume} л\n"
            f"Репутация: {user.reputation}\n"
            f"PRO: {pro_status}"
        )
        await message.answer(text, reply_markup=main_menu_keyboard())

@router.message(F.text == "Изменить город")
async def change_city_start(message: types.Message, state: FSMContext):
    await state.set_state(ProfileStates.waiting_city)
    await message.answer("Введите название города:", reply_markup=back_to_menu_keyboard())

@router.message(ProfileStates.waiting_city, F.text)
async def change_city_set(message: types.Message, state: FSMContext):
    if message.text == "◀️ Назад":
        await state.clear()
        await message.answer("Главное меню:", reply_markup=main_menu_keyboard())
        return
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала /start")
            await state.clear()
            return
        city = await get_city_by_name(db, message.text.strip())
        if not city:
            await message.answer("Город не найден. Попробуйте другой.")
            return
        await update_user(db, user, city_id=city.id)
        await message.answer(f"Город изменён на {city.name}", reply_markup=main_menu_keyboard())
        await state.clear()
