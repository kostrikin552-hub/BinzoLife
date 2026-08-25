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

router = Router()

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    args = message.text.split()
    user_id = message.from_user.id
    username = message.from_user.username

    # Проверяем наличие параметров (реферальная ссылка или pro)
    if len(args) > 1:
        if args[1].startswith("ref_"):
            ref_code = args[1][4:]
        elif args[1] == "pro":
            from handlers.payments import show_pro_info
            await show_pro_info(message)
            return
        else:
            ref_code = None
    else:
        ref_code = None

    async with AsyncSessionLocal() as db:
        user = await get_user(db, user_id)
        if not user:
            # Новый пользователь
            user = await create_user(db, user_id, username)
            # Применяем реферальный код, если есть
            if ref_code:
                await apply_referral(db, user.id, ref_code)
            # Устанавливаем город по умолчанию (Красноярск)
            city = await get_city_by_name(db, "Красноярск")
            if city:
                user.city_id = city.id
                await db.commit()

            # Короткое продающее приветствие для новых пользователей
            await message.answer(
                "📖 **BinzoLife за 3 шага:**\n\n"
                "1. Нажми «Найти заправку» → выбери топливо → получи АЗС с ценой, наличием и маршрутом.\n"
                "2. В критической ситуации нажми «Бензин заканчивается!» — я найду топливо за 10 секунд.\n"
                "3. Подключи PRO, чтобы получать уведомления о снижении цен и не упускать выгоду.\n\n"
                "💰 Экономь до 500 ₽ за заправку. Начни сейчас → «Найти заправку».",
                reply_markup=welcome_back_keyboard(),
                parse_mode="HTML"
            )
            return

        # Возвращающийся пользователь
        if user.city_id:
            await message.answer(
                "⛽ С возвращением! Где ищем заправку сегодня?\n\n"
                "💰 Экономь до 500 ₽ за раз и не стой в очередях.",
                reply_markup=welcome_back_keyboard()
            )
            return

        # Если город не выбран
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
        f"✅ Город {city_name} сохранён!\n"
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
