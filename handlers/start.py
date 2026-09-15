# handlers/start.py — ОБНОВЛЁННАЯ ВЕРСИЯ
import html
import logging
from aiogram import Router, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_user, get_city_by_name, get_city_by_id,
    apply_referral, get_user_by_referral_code,
    set_user_timezone, find_nearest_city, save_user_location,
    commit_or_rollback, find_or_create_city_by_query
)
from database.models import FuelType
from handlers.find import perform_search
from handlers.payments import show_pro_info
from keyboards.reply import (
    main_menu_keyboard, welcome_back_keyboard, request_geo_or_city_keyboard
)
from utils.geocoder import reverse_geocode
from states.city import CitySelectStates

router = Router()
logger = logging.getLogger(__name__)


@router.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await process_start(message, state)


# ====================== ПРИОРИТЕТНЫЙ ХЕНДЛЕР: ГЕОЛОКАЦИЯ ПРИ ВЫБОРЕ ГОРОДА ======================
@router.message(CitySelectStates.waiting_city_name, F.location)
async def process_location_while_selecting_city(message: types.Message, state: FSMContext):
    """Геолокация используется ТОЛЬКО при выборе/смене города."""
    lat = message.location.latitude
    lon = message.location.longitude
    user_id = message.from_user.id

    async with AsyncSessionLocal() as db:
        user = await get_user(db, user_id)
        if not user:
            user = await create_user(db, user_id, message.from_user.username, message.from_user.first_name)

        await save_user_location(db, user.id, lat, lon)
        await set_user_timezone(db, user.id, lat, lon)

        city = await find_nearest_city(db, lat, lon, radius_km=100.0)
        if not city:
            city_name = await reverse_geocode(lat, lon)
            if city_name:
                city = await find_or_create_city_by_query(db, city_name)

        if city:
            user.city_id = city.id
            user.last_lat = lat
            user.last_lon = lon
            await commit_or_rollback(db)

            data = await state.get_data()
            origin = data.get("origin")
            await state.clear()

            await message.answer(
                f"✅ Город успешно определён: <b>{html.escape(city.name)}</b>!\n"
                "Все цены и фильтры настроены.",
                reply_markup=main_menu_keyboard(),
                parse_mode="HTML"
            )

            if origin == "profile":
                from handlers.profile import show_profile
                await show_profile(message, user_telegram_id=user_id)
            else:
                await state.update_data(
                    city_id=city.id,
                    lat=lat,
                    lon=lon,
                    fuel_type=FuelType.AI_95,
                    sort_mode="rating"
                )
                await perform_search(message, state, user_telegram_id=user_id)
        else:
            await message.answer(
                "❌ Не удалось определить город по этим координатам.\n"
                "Пожалуйста, напишите название города текстом (например: <i>Казань</i>):",
                reply_markup=request_geo_or_city_keyboard(),
                parse_mode="HTML"
            )


# ====================== СТАРТОВЫЙ СЦЕНАРИЙ ======================
async def process_start(message: types.Message, state: FSMContext):
    args = message.text.split() if message.text else []
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

            # Город по умолчанию — Красноярск
            city = await get_city_by_name(db, "Красноярск")
            if city:
                user.city_id = city.id
                user.last_lat = city.latitude
                user.last_lon = city.longitude
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
                f"🎁 <b>Подарок на старт:</b> вам открыт <b>полный PRO-доступ на 3 дня</b>!\n\n"
                f"📍 <b>Ваш город:</b> Красноярск\n"
                f"<i>Если это не так, смените его в разделе «👤 Профиль» → «🏙 Изменить город».</i>\n\n"
                f"👇 <i>Нажмите «⛽ Найти заправку», чтобы увидеть лучшую цену рядом:</i>"
            )
            await message.answer(welcome_text, reply_markup=main_menu_keyboard(), parse_mode="HTML")
            return

        if user.city_id:
            user_name = html.escape(first_name or "водитель")
            await message.answer(
                f"⛽ <b>{user_name}</b>, с возвращением! Где ищем заправку сегодня?\n\n"
                f"💰 Экономь до 500 ₽ за раз и не стой в очередях.\n"
                f"📍 Сменить город можно в разделе «👤 Профиль».",
                reply_markup=welcome_back_keyboard(),
                parse_mode="HTML"
            )
            return

        await message.answer(
            "⛽ Привет! Я — BinzoLife.\n\n"
            "Сэкономь до 500 ₽ на одной заправке и забудь про очереди.\n\n"
            "📍 Для начала определим твой город:",
            reply_markup=request_geo_or_city_keyboard()
        )


# ====================== ОБРАБОТЧИКИ ВВОДА ГОРОДА ТЕКСТОМ ======================
@router.callback_query(F.data == "input_city_name")
async def input_city_name(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(CitySelectStates.waiting_city_name)
    try:
        await callback.message.edit_text(
            "✏️ Введите название вашего города (например: <b>Казань</b>, <b>Тюмень</b>, <b>СПб</b>):",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
            ])
        )
    except Exception:
        await callback.message.answer(
            "✏️ Введите название вашего города:",
            parse_mode="HTML"
        )


@router.message(CitySelectStates.waiting_city_name, F.text)
async def process_text_city(message: types.Message, state: FSMContext):
    text_val = message.text.strip()

    if text_val in ["✏️ Написать город текстом", "🏙 Выбрать город вручную"]:
        await message.answer("✏️ Пожалуйста, напишите название города сообщением:")
        return

    if text_val in ["◀️ В главное меню", "◀️ Назад", "/start"]:
        await state.clear()
        await message.answer("Главное меню:", reply_markup=main_menu_keyboard())
        return

    async with AsyncSessionLocal() as db:
        city = await find_or_create_city_by_query(db, text_val)
        if not city:
            await message.answer(
                f"❌ Не удалось найти город «{html.escape(text_val)}».\n"
                "Пожалуйста, проверьте написание или отправьте геолокацию через кнопку/скрепку:",
                reply_markup=request_geo_or_city_keyboard(),
                parse_mode="HTML"
            )
            return

        user = await get_user(db, message.from_user.id)
        if not user:
            user = await create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)

        user.city_id = city.id
        user.last_lat = city.latitude
        user.last_lon = city.longitude
        await commit_or_rollback(db)

        data = await state.get_data()
        origin = data.get("origin")
        await state.clear()

        await message.answer(
            f"✅ Город установлен: <b>{html.escape(city.name)}</b>!\n"
            "Теперь все цены и поиск АЗС настроены под ваш регион.",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML"
        )

        if origin == "profile":
            from handlers.profile import show_profile
            await show_profile(message, user_telegram_id=message.from_user.id)


@router.message(F.text == "✏️ Написать город текстом")
async def manual_city_text_prompt(message: types.Message, state: FSMContext):
    await state.set_state(CitySelectStates.waiting_city_name)
    await message.answer(
        "✏️ Введите название вашего города (например: <b>Москва</b>, <b>Казань</b>, <b>Екатеринбург</b>):",
        reply_markup=request_geo_or_city_keyboard(),
        parse_mode="HTML"
    )


@router.message(F.text == "ℹ️ Как это работает")
async def how_it_works(message: types.Message):
    text = (
        "📖 <b>Как BinzoLife экономит время и деньги:</b>\n\n"
        "1️⃣ <b>Поиск АЗС за 5 секунд</b>\n"
        "Нажмите «⛽ Найти заправку» — выберите топливо и сортировку, бот найдёт ближайшие заправки с минимальной ценой.\n\n"
        "2️⃣ <b>Смена города</b>\n"
        "Если вы переехали или хотите посмотреть цены в другом городе — откройте «👤 Профиль» → «🏙 Изменить город».\n\n"
        "3️⃣ <b>Сообщайте цены</b>\n"
        "За актуализацию цен вы получаете репутацию и бесплатные дни PRO.\n\n"
        "💡 <i>Если кнопка «📍 Определить город по GPS» на ПК выдаёт ошибку, просто введите город текстом или отправьте точку через скрепку 📎.</i>"
    )
    await message.answer(text, parse_mode="HTML", reply_markup=welcome_back_keyboard())
