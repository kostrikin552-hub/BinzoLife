# handlers/start.py — ПОЛНАЯ ВЕРСИЯ С ИНСТРУКЦИЕЙ
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.session import AsyncSessionLocal
from database.crud import get_user, create_user, get_city_by_name, apply_referral, get_user_by_referral_code
from keyboards.inline import welcome_back_keyboard, city_choice_keyboard, popular_cities_keyboard, main_menu_keyboard

router = Router()

CHANNEL_LINK = "https://t.me/BinzoLife_News"

@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await process_start(message, state)

async def process_start(message: types.Message, state: FSMContext):
    args = message.text.split()
    user_id = message.from_user.id
    username = message.from_user.username

    ref_code = None
    if len(args) > 1:
        if args[1].startswith("ref_"):
            ref_code = args[1][4:]
        elif args[1] == "pro":
            from handlers.payments import show_pro_info
            await show_pro_info(message)
            return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, user_id)
        if not user:
            user = await create_user(db, user_id, username)

            if ref_code:
                referrer = await get_user_by_referral_code(db, ref_code)
                if referrer and referrer.telegram_id != user.telegram_id:
                    await apply_referral(db, user.id, ref_code)

            city = await get_city_by_name(db, "Красноярск")
            if city:
                user.city_id = city.id
                await db.commit()

            await message.answer(
                "👋 Привет! Я **BinzoLife** — твой личный топливный ассистент.\n\n"
                "⛽ Я покажу самые выгодные заправки в твоём городе с учётом расхода на дорогу.\n"
                "💰 В среднем пользователи экономят **от 300 до 800 ₽ с каждого бака**!\n\n"
                "📍 <b>Как пользоваться геолокацией:</b>\n"
                "• Нажми «📍 Отправить геолокацию» — я найду заправки рядом с тобой.\n"
                "• Чтобы не отправлять её каждый раз, включи <b>трансляцию геопозиции</b> в Telegram:\n"
                "  ➤ Нажми на скрепку 📎 → «Местоположение» → «Отправить мою текущую геопозицию»\n"
                "  ➤ Внизу появится кнопка «Включить трансляцию» — нажми её и выбери время\n"
                "  ➤ Теперь я всегда буду знать, где ты находишься!\n\n"
                "🎁 <b>Бонус:</b> ты получаешь <b>3 дня PRO</b> бесплатно при первом поиске.\n\n"
                "👇 Нажми кнопку, чтобы начать экономить:",
                reply_markup=welcome_back_keyboard(),
                parse_mode="HTML"
            )
            return

        if user.city_id:
            await message.answer(
                "⛽ С возвращением! Где ищем заправку сегодня?\n\n"
                "📍 Если хочешь, чтобы я запомнил твою геопозицию, нажми «📍 Отправить геолокацию».\n"
                "💰 Экономь до 500 ₽ за раз и не стой в очередях.",
                reply_markup=welcome_back_keyboard()
            )
            return

        await message.answer(
            "⛽ Привет! Я — BinzoLife.\n\n"
            "Сэкономь до 500 ₽ на одной заправке и забудь про очереди.\n\n"
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
        f"✅ Город {city_name} сохранён! Теперь я буду показывать цены именно для этого города.\n\n"
        "Что делаем?",
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
