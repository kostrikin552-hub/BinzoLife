from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.session import AsyncSessionLocal
from database.crud import get_user, get_city_by_name, is_user_pro, get_user_achievements, get_referral_link
from keyboards.reply import main_menu_keyboard
from keyboards.inline import popular_cities_keyboard
from services.subscription import format_pro_until

router = Router()

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

        achievements = await get_user_achievements(db, user.id)
        ach_text = "\n".join([f"🏅 {a.achievement_type} (бонус: +{a.bonus_days_granted} дн PRO)" for a in achievements]) if achievements else "Нет достижений"

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
async def change_city(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "📍 Выбери новый город из списка:",
        reply_markup=popular_cities_keyboard(with_back=True)
    )

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
            "Пригласите друга, и вы оба получите +3 дня PRO (если друг совершит хотя бы один поиск).",
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
