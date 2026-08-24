import logging
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from database.session import AsyncSessionLocal
from database.crud import get_user, create_user, get_city_by_name, apply_referral
from keyboards.inline import (
    welcome_back_keyboard,
    city_choice_keyboard,
    popular_cities_keyboard,
    main_menu_keyboard
)

logger = logging.getLogger(__name__)
router = Router()

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    args = message.text.split()
    user_id = message.from_user.id
    username = message.from_user.username

    # ---- Обработка специальных параметров ----
    if len(args) > 1:
        if args[1].startswith("ref_"):
            # Реферальная ссылка: применяем код после создания пользователя (если новый)
            ref_code = args[1][4:]
        elif args[1] == "pro":
            # Прямой переход на страницу PRO
            from handlers.payments import show_pro_info
            await show_pro_info(message)
            return
        else:
            ref_code = None
    else:
        ref_code = None

    # ---- Основная логика приветствия ----
    async with AsyncSessionLocal() as db:
        user = await get_user(db, user_id)
        if not user:
            user = await create_user(db, user_id, username)
            # Применяем реферальный код, если есть
            if ref_code:
                await apply_referral(db, user.id, ref_code)
            # Город по умолчанию — Красноярск (если есть)
            city = await get_city_by_name(db, "Красноярск")
            if city:
                user.city_id = city.id
                await db.commit()
        else:
            # Если пользователь уже есть и перешёл по реферальной ссылке — попробуем применить
            if ref_code:
                await apply_referral(db, user.id, ref_code)

        # Если город уже выбран — короткое приветствие
        if user.city_id:
            await message.answer(
                "⛽ С возвращением! Где ищем заправку сегодня?\n\n"
                "💰 Экономь до 500 ₽ за раз и не стой в очередях.",
                reply_markup=welcome_back_keyboard()
            )
            return

        # Город не выбран — полное приветствие с выбором города
        await message.answer(
            "⛽ Привет! Я — BinzoLife.\n\n"
            "Я знаю, где прямо сейчас есть 95-й бензин, по какой цене и сколько до него ехать.\n\n"
            "Сэкономь до 500 ₽ на одной заправке и забудь про очереди.\n\n"
            "Что я умею:\n"
            "✅ Найти ближайшую АЗС с топливом (даже в час пик)\n"
            "✅ Показать самую дешёвую цену в твоём районе\n"
            "✅ Построить маршрут за 1 клик\n\n"
            "📍 Для начала выбери свой город:",
            reply_markup=city_choice_keyboard()
        )

# ---------- Обработчики выбора города ----------
@router.callback_query(F.data == "city_list")
async def city_list(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "📍 Выбери свой город из списка:",
        reply_markup=popular_cities_keyboard()
    )

@router.callback_query(lambda c: c.data.startswith("city_select_"))
async def city_select(callback: types.CallbackQuery):
    city_name = callback.data.split("_")[2]
    await callback.answer()

    async with AsyncSessionLocal() as db:
        city = await get_city_by_name(db, city_name)
        if not city:
            await callback.message.edit_text(
                f"❌ Город '{city_name}' пока не добавлен в базу.\n"
                "Пожалуйста, выберите другой город из списка.",
                reply_markup=popular_cities_keyboard()
            )
            return

        user = await get_user(db, callback.from_user.id)
        if user:
            user.city_id = city.id
            await db.commit()

    await callback.message.delete()
    await callback.message.answer(
        f"✅ Город {city.name} сохранён!\n"
        "Теперь я буду искать заправки рядом с тобой.\n\n"
        "Нажми «Найти заправку», чтобы начать.",
        reply_markup=welcome_back_keyboard()
    )

@router.callback_query(F.data == "search_now")
async def search_now(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await callback.message.answer(
        "🚀 Для поиска заправки сначала выбери город.\n"
        "Нажми /start, чтобы выбрать город.",
        reply_markup=main_menu_keyboard()
    )
