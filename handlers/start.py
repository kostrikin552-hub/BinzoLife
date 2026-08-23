import logging
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from database.session import AsyncSessionLocal
from database.crud import get_user, create_user, get_city_by_name
from utils.geo import get_city_by_ip
from keyboards.inline import welcome_back_keyboard, city_choice_keyboard, main_menu_keyboard

logger = logging.getLogger(__name__)
router = Router()

class CityStates(StatesGroup):
    waiting_city = State()

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username

    async with AsyncSessionLocal() as db:
        user = await get_user(db, user_id)
        if not user:
            user = await create_user(db, user_id, username)

        if user.city_id:
            await message.answer(
                "⛽ С возвращением! Где ищем заправку сегодня?\n\n"
                "💰 Экономь до 500 ₽ за раз и не стой в очередях.",
                reply_markup=welcome_back_keyboard()
            )
            return

        await message.answer(
            "⛽ Привет! Я — BinzoLife.\n\n"
            "Я знаю, где прямо сейчас есть 95-й бензин, по какой цене и сколько до него ехать.\n\n"
            "Сэкономь до 500 ₽ на одной заправке и забудь про очереди.\n\n"
            "Что я умею:\n"
            "✅ Найти ближайшую АЗС с топливом (даже в час пик)\n"
            "✅ Показать самую дешёвую цену в твоём районе\n"
            "✅ Построить маршрут за 1 клик\n\n"
            "Где ты находишься?\n"
            "Выбери свой город — и я покажу лучшие варианты рядом с тобой.",
            reply_markup=city_choice_keyboard()
        )

@router.callback_query(F.data == "city_by_ip")
async def city_by_ip(callback: types.CallbackQuery):
    await callback.answer("Определяем город...")
    user_id = callback.from_user.id

    ip = "8.8.8.8"  # заглушка
    city_name = await get_city_by_ip(ip)

    if not city_name:
        await callback.message.edit_text(
            "❌ Не удалось определить город по IP. Пожалуйста, введите название вручную.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="city_manual")]
            ])
        )
        return

    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, city_name)
        if not city:
            await callback.message.edit_text(
                f"❌ Город '{city_name}' не найден в базе. Пожалуйста, введите вручную.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="city_manual")]
                ])
            )
            return

        user = await get_user(db, user_id)
        if user:
            user.city_id = city.id
            await db.commit()

    # Удаляем старое сообщение и отправляем новое с Reply-клавиатурой
    await callback.message.delete()
    await callback.message.answer(
        f"✅ Город {city.name} определён!\n"
        "Теперь я буду искать заправки рядом с тобой.\n\n"
        "Нажми «Найти заправку», чтобы начать.",
        reply_markup=welcome_back_keyboard()
    )

@router.callback_query(F.data == "city_manual")
async def city_manual(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(CityStates.waiting_city)
    await callback.message.edit_text(
        "📍 Введи название своего города (например, «Красноярск» или «Москва»):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_city")]
        ])
    )

@router.callback_query(F.data == "search_now")
async def search_now(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await callback.message.answer(
        "🚀 Для поиска заправки нажми «Найти заправку» в главном меню.\n\n"
        "Если город ещё не выбран — сначала выбери его через профиль или команду /start.",
        reply_markup=main_menu_keyboard()
    )

@router.message(CityStates.waiting_city, F.text)
async def city_manual_input(message: types.Message, state: FSMContext):
    city_name = message.text.strip()
    if not city_name:
        await message.answer("❌ Название города не может быть пустым. Попробуй снова.")
        return

    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, city_name)
        if not city:
            await message.answer(
                f"❌ Город '{city_name}' не найден в базе.\n"
                "Пожалуйста, введите существующий город или обратитесь к администратору.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Попробовать снова", callback_data="city_manual")]
                ])
            )
            return

        user = await get_user(db, message.from_user.id)
        if user:
            user.city_id = city.id
            await db.commit()

    await state.clear()
    await message.answer(
        f"✅ Город {city.name} сохранён!\n"
        "Теперь я буду искать заправки рядом с тобой.\n\n"
        "Нажми «Найти заправку», чтобы начать.",
        reply_markup=welcome_back_keyboard()
    )

@router.callback_query(F.data == "cancel_city")
async def cancel_city(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        "❌ Выбор города отменён.\n\n"
        "Ты можешь выбрать город позже через профиль или при первом поиске.",
        reply_markup=welcome_back_keyboard()
    )
