from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from database.session import AsyncSessionLocal
from database.crud import (
    get_user, update_user, get_city_by_name, is_user_pro,
    get_user_achievements, get_referral_link, get_user_economy_total
)
from database.models import FuelType
from keyboards.reply import main_menu_keyboard
from services.subscription import format_pro_until

router = Router()

class ProfileStates(StatesGroup):
    waiting_city = State()

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

        fuel_display = user.default_fuel.value if hasattr(user.default_fuel, 'value') else str(user.default_fuel)

        # достижения
        achievements = await get_user_achievements(db, user.id)
        ach_text = "\n".join([f"🏅 {a.achievement_type} (бонус: +{a.bonus_days_granted} дн PRO)" for a in achievements]) if achievements else "Нет достижений"

        # экономия
        total_saved = user.total_saved or 0.0

        text = (
            f"👤 Профиль\n"
            f"ID: {user.telegram_id}\n"
            f"Город: {city_name}\n"
            f"Топливо по умолчанию: {fuel_display}\n"
            f"Объём бака: {user.tank_volume} л\n"
            f"Репутация: {user.reputation}\n"
            f"PRO: {pro_status}\n"
            f"💰 Всего сэкономлено: {total_saved:.2f} ₽\n"
            f"🏅 Достижения:\n{ach_text}"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏙 Изменить город", callback_data="change_city")],
            [InlineKeyboardButton(text="🔗 Реферальная ссылка", callback_data="referral")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]
        ])

        await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "change_city")
async def change_city_start(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(ProfileStates.waiting_city)
    await callback.message.edit_text(
        "Введите название города (например, Красноярск):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_city")]
        ])
    )

@router.callback_query(F.data == "cancel_city")
async def cancel_city(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.edit_text("Изменение города отменено.")
    await show_profile(callback.message)

@router.message(ProfileStates.waiting_city, F.text)
async def set_city(message: types.Message, state: FSMContext):
    city_name = message.text.strip()
    if not city_name:
        await message.answer("Название города не может быть пустым. Попробуйте снова.")
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала выполните /start")
            await state.clear()
            return

        city = await get_city_by_name(db, city_name)
        if not city:
            await message.answer(
                f"❌ Город '{city_name}' не найден в базе.\n"
                "Пожалуйста, введите существующий город или обратитесь к администратору."
            )
            return

        user.city_id = city.id
        await db.commit()
        await db.refresh(user)

        await message.answer(f"✅ Город изменён на {city.name}.")
        await state.clear()
        await show_profile(message)

@router.callback_query(F.data == "referral")
async def referral_link(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.answer("Сначала /start")
            return
        link = await get_referral_link(db, user)
        await callback.message.edit_text(
            f"🔗 Ваша реферальная ссылка:\n{link}\n\n"
            "Пригласите друга, и вы оба получите +1 день PRO (если друг совершит хотя бы один поиск).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]
            ])
        )

@router.callback_query(F.data == "back_to_profile")
async def back_to_profile(callback: types.CallbackQuery):
    await callback.answer()
    await show_profile(callback.message)

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard())
