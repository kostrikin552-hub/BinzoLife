# handlers/start.py — ИСПРАВЛЕННАЯ ВЕРСИЯ (импорты клавиатур)
import html
import logging
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_user, get_city_by_name, apply_referral,
    get_user_by_referral_code, get_user_by_id, set_user_timezone,
    find_nearest_city, save_user_location, commit_or_rollback
)
from database.models import FuelType
from handlers.find import perform_search
from handlers.payments import show_pro_info
from keyboards.reply import main_menu_keyboard, welcome_back_keyboard
from keyboards.inline import city_choice_keyboard, popular_cities_keyboard

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await process_start(message, state)


@router.message(F.location)
async def process_instant_onboarding_geo(message: types.Message, state: FSMContext):
    """Автоопределение города по GPS и переход к поиску."""
    lat = message.location.latitude
    lon = message.location.longitude
    user_id = message.from_user.id

    async with AsyncSessionLocal() as db:
        user = await get_user(db, user_id)
        if not user:
            user = await create_user(db, user_id, message.from_user.username, message.from_user.first_name)

        await save_user_location(db, user.id, lat, lon)
        await set_user_timezone(db, user.id, lat, lon)

        nearest_city = await find_nearest_city(db, lat, lon)
        if nearest_city:
            user.city_id = nearest_city.id
            await commit_or_rollback(db)
            await message.answer(
                f"📍 Определили ваш регион: <b>{html.escape(nearest_city.name)}</b>!\n"
                f"Ищем заправки с минимальной ценой вокруг вас...",
                parse_mode="HTML"
            )
            await state.update_data(
                city_id=nearest_city.id,
                lat=lat,
                lon=lon,
                fuel_type=FuelType.AI_95,
                sort_mode="rating"
            )
            await perform_search(message, state)
            return
        else:
            await message.answer(
                "❌ Не удалось определить ваш город автоматически.\n"
                "Пожалуйста, выберите город из списка:",
                reply_markup=city_choice_keyboard()
            )


async def process_start(message: types.Message, state: FSMContext):
    args = message.text.split()
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name

    ref_code = None
    if len(args) > 1:
        if args[1].startswith("ref_"):
            ref_code = args[1][4:]
        elif args[1] == "pro":
            await show_pro_info(message)
            return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, user_id)
        if not user:
            user = await create_user(db, user_id, username, first_name)

            if ref_code:
                referrer = await get_user_by_referral_code(db, ref_code)
                if referrer and referrer.telegram_id != user.telegram_id:
                    await apply_referral(db, user.id, ref_code)

            city = await get_city_by_name(db, "Красноярск")
            if city:
                user.city_id = city.id
                await commit_or_rollback(db)

            user_name = html.escape(first_name or "водитель")
            welcome_text = (
                f"👋 Рады видеть вас, <b>{user_name}</b>!\n\n"
                f"Я <b>BinzoLife</b> — ваш персональный топливный штурман. "
                f"Я нахожу честные цены на стелах АЗС и считаю, где заправиться <b>действительно выгодно</b> "
                f"с учётом расхода на дорогу.\n\n"
                f"📊 <b>В среднем наши водители берегут:</b>\n"
                f"• <code>250 – 480 ₽</code> с каждого полного бака\n"
                f"• <code>до 3 500 ₽</code> семейного бюджета в месяц\n\n"
                f"🎁 <b>Подарок на старт:</b> вам открыт <b>полный PRO-доступ на 3 дня</b> "
                f"(радар очередей, графики и максимальный радиус)!\n\n"
                f"👇 <i>Нажмите кнопку ниже, чтобы увидеть лучшую цену рядом за 2 секунды:</i>"
            )
            geo_kb = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="📍 Найти самый дешевый бензин рядом", request_location=True)],
                    [KeyboardButton(text="🏙 Выбрать город вручную"), KeyboardButton(text="ℹ️ Как это работает")],
                    [KeyboardButton(text="👤 Профиль")]
                ],
                resize_keyboard=True,
                one_time_keyboard=False
            )
            await message.answer(welcome_text, reply_markup=geo_kb, parse_mode="HTML")
            return

        if user.city_id:
            user_name = html.escape(first_name or "водитель")
            await message.answer(
                f"⛽ <b>{user_name}</b>, с возвращением! Где ищем заправку сегодня?\n\n"
                f"💰 Экономь до 500 ₽ за раз и не стой в очередях.\n"
                f"📍 Если хочешь, чтобы я запомнил твою геопозицию, нажми «📍 Отправить геолокацию».",
                reply_markup=welcome_back_keyboard(),
                parse_mode="HTML"
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
            await commit_or_rollback(db)

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


@router.message(F.text == "🏙 Выбрать город вручную")
async def manual_city_choice(message: types.Message):
    await message.answer(
        "📍 Выберите город из списка:",
        reply_markup=city_choice_keyboard()
    )


@router.message(F.text == "ℹ️ Как это работает")
async def how_it_works(message: types.Message):
    text = (
        "📖 <b>Как BinzoLife сэкономит тебе время и деньги:</b>\n\n"
        "1️⃣ <b>Найди АЗС за 10 секунд</b>\n"
        "Нажми «📍 Найти самый дешевый бензин рядом» или «⛽ Найти заправку» — я покажу лучшие варианты.\n\n"
        "2️⃣ <b>Бензин на нуле?</b>\n"
        "Нажми «🚨 Бензин заканчивается!» — я найду ближайшую АЗС с топливом за 10 секунд.\n\n"
        "3️⃣ <b>Помогай другим водителям</b>\n"
        "Сообщай актуальные цены через «✏️ Сообщить цену» — получай репутацию и бонусные дни PRO.\n\n"
        "4️⃣ <b>Не упускай выгоду</b>\n"
        "С PRO ты будешь получать уведомления о снижении цен и появлении топлива на любимых АЗС.\n\n"
        "💡 Все данные обновляются в реальном времени и имеют временную метку — я честен с тобой."
    )
    await message.answer(text, parse_mode="HTML", reply_markup=welcome_back_keyboard())
