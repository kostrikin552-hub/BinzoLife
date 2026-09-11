# handlers/profile.py — ПОЛНАЯ ФИНАЛЬНАЯ ВЕРСИЯ
import html
import logging
from datetime import datetime, timedelta, timezone, date
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_user, get_city_by_name, is_user_pro, get_user_achievements,
    get_referral_link, get_user_search_count, get_user_referrals_count,
    get_next_achievement_progress, get_missed_price_drops,
    get_potential_saving, get_user_search_history,
    set_silent_hours, clear_silent_hours, is_silent_hours_now,
    set_user_timezone, save_user_location, find_nearest_city,
    commit_or_rollback, find_or_create_city_by_query
)
from database.models import FuelType
from keyboards.reply import main_menu_keyboard, request_geo_or_city_keyboard
from keyboards.inline import pro_purchase_keyboard, get_fuel_selection_keyboard
from services.subscription import format_pro_until, check_pro
from utils.geocoder import reverse_geocode

router = Router()
logger = logging.getLogger(__name__)

LEVELS = [
    (0, "🥉 Наблюдатель", 0),
    (5, "🥈 Местный Штурман", 1),
    (20, "🥇 Эксперт Дорог", 3),
    (50, "👑 Топливный Барон", 7),
    (100, "🏆 Легенда Автотрасс", 14),
]

class CitySelectStates(StatesGroup):
    waiting_city_name = State()

def get_user_level(reputation: int) -> tuple:
    for i, (threshold, name, bonus) in enumerate(LEVELS):
        if reputation < threshold:
            next_threshold = LEVELS[i][0] if i < len(LEVELS) else None
            next_name = LEVELS[i][1] if i < len(LEVELS) else None
            return name, bonus, next_threshold - reputation if next_threshold else 0, next_name
    return LEVELS[-1][1], LEVELS[-1][2], 0, None

def generate_progress_bar(current: int, target: int, length: int = 10) -> str:
    if target <= 0:
        return "■" * length
    filled = min(length, int((current / target) * length))
    empty = length - filled
    return f"[{'■' * filled}{'□' * empty}]"


@router.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message, user_telegram_id: int = None):
    target_user_id = user_telegram_id if user_telegram_id else message.from_user.id
    async with AsyncSessionLocal() as db:
        user = await get_user(db, target_user_id)
        if not user:
            await message.answer("Сначала выполните /start")
            return

        city_name = html.escape(user.city.name) if user.city else "Не задан"
        fuel_display = getattr(user.default_fuel, "value", "АИ-95") if user.default_fuel else "АИ-95"
        tank_volume = user.tank_volume or 50

        is_pro = await check_pro(user.telegram_id)
        if is_pro and user.pro_until:
            days_left = (user.pro_until - datetime.now(timezone.utc)).days
            status_text = f"✅ Активен до {user.pro_until.strftime('%d.%m.%Y %H:%M')} (осталось {days_left} дн.)"
        else:
            status_text = "❌ Не активен"

        reputation = user.reputation or 0
        level_name, level_bonus, rep_to_next, next_level_name = get_user_level(reputation)
        progress_bar = generate_progress_bar(reputation, rep_to_next + reputation) if rep_to_next > 0 else "■" * 10

        level_line = (
            f"🎖 <b>Звание:</b> {level_name}\n"
            f"⭐️ <b>Репутация:</b> {reputation} баллов\n"
            f"📈 <b>До нового ранга:</b> {progress_bar} <i>({reputation}/{reputation + rep_to_next})</i>"
            if rep_to_next > 0
            else f"🎖 <b>Звание:</b> {level_name}\n⭐️ <b>Репутация:</b> {reputation} баллов (Максимум!)"
        )

        search_count = await get_user_search_count(db, user.id)
        ref_count = user.invited_count or 0

        text = (
            f"👤 <b>Кабинет водителя</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Сохранено:</b> <code>{user.total_saved or 0:,.0f} ₽</code>\n"
            f"{level_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Параметры авто:</b>\n"
            f"• Город: <b>{city_name}</b>\n"
            f"• Топливо: <b>{fuel_display}</b>\n"
            f"• Объём бака: <b>{tank_volume} л</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 <b>Статус:</b> {status_text}\n"
            f"👥 <b>Рефералов:</b> {ref_count}\n"
            f"🔍 <b>Поисков:</b> {search_count}\n"
        )

        kb_buttons = [
            [InlineKeyboardButton(text="🏙 Изменить город", callback_data="change_city"),
             InlineKeyboardButton(text="⛽ Изменить топливо", callback_data="change_fuel")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="stats")],
            [InlineKeyboardButton(text="📜 История поисков", callback_data="search_history")],
            [InlineKeyboardButton(text="🔇 Настройка тишины", callback_data="silent_settings")],
            [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="referral_hub")],
        ]
        if not is_pro:
            kb_buttons.append([InlineKeyboardButton(text="🔥 Оформить PRO от 50 ₽", callback_data="buy_pro")])
        kb_buttons.append([InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")])

        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons), parse_mode="HTML")


@router.callback_query(F.data == "change_city")
async def change_city_request(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(CitySelectStates.waiting_city_name)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "🏙 <b>Смена города</b>\n\n"
        "Отправьте геолокацию кнопкой ниже или напишите название города (например: <i>Москва</i>, <i>Казань</i>):",
        reply_markup=request_geo_or_city_keyboard(),
        parse_mode="HTML"
    )


@router.message(CitySelectStates.waiting_city_name, F.location)
async def process_location_city(message: types.Message, state: FSMContext):
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
            await state.clear()
            await message.answer(
                f"✅ Город установлен: <b>{html.escape(city.name)}</b>!\nКоординаты обновлены.",
                reply_markup=main_menu_keyboard(),
                parse_mode="HTML"
            )
            await show_profile(message, user_telegram_id=user_id)
        else:
            await message.answer("❌ Не удалось определить город. Напишите его текстом:")


@router.message(CitySelectStates.waiting_city_name, F.text)
async def process_text_city(message: types.Message, state: FSMContext):
    text_val = message.text.strip()
    if text_val in ["◀️ В главное меню", "◀️ Назад", "/start"]:
        await state.clear()
        await message.answer("Главное меню:", reply_markup=main_menu_keyboard())
        return

    async with AsyncSessionLocal() as db:
        city = await find_or_create_city_by_query(db, text_val)
        if not city:
            await message.answer(f"❌ Город «{html.escape(text_val)}» не найден. Проверьте написание:")
            return

        user = await get_user(db, message.from_user.id)
        if not user:
            user = await create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)

        user.city_id = city.id
        user.last_lat = city.latitude
        user.last_lon = city.longitude
        await commit_or_rollback(db)

        await state.clear()
        await message.answer(f"✅ Город установлен: <b>{html.escape(city.name)}</b>!", reply_markup=main_menu_keyboard(), parse_mode="HTML")
        await show_profile(message, user_telegram_id=message.from_user.id)


@router.callback_query(F.data == "change_fuel")
async def change_fuel(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        default_fuel = getattr(user.default_fuel, "value", "АИ-95") if user and user.default_fuel else "АИ-95"
        kb = get_fuel_selection_keyboard(default_fuel)
    await callback.message.edit_text("⛽ Выберите топливо по умолчанию:", reply_markup=kb)


@router.callback_query(lambda c: c.data.startswith("fuel_"))
async def set_fuel(callback: types.CallbackQuery):
    fuel_type = callback.data.split("_")[1]
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if user:
            setattr(user, 'default_fuel', FuelType(fuel_type))
            await commit_or_rollback(db)
    await callback.answer(f"✅ Топливо {fuel_type} сохранено")
    await show_profile(callback.message, user_telegram_id=callback.from_user.id)


@router.callback_query(F.data == "stats")
async def show_stats(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        search_count = await get_user_search_count(db, user.id)
        text = (
            f"📊 <b>Ваша статистика</b>\n\n"
            f"🔍 Поисков: {search_count}\n"
            f"💰 Сэкономлено: {user.total_saved or 0:.2f} ₽\n"
            f"⭐ Репутация: {user.reputation}\n"
        )
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]]),
            parse_mode="HTML"
        )


@router.callback_query(F.data == "back_to_profile")
async def back_to_profile(callback: types.CallbackQuery):
    await callback.answer()
    await show_profile(callback.message, user_telegram_id=callback.from_user.id)


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "search_history")
async def show_search_history(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        history = await get_user_search_history(db, user.id, limit=10)
        if not history:
            await callback.message.edit_text(
                "📜 История пока пуста.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]])
            )
            return
        text = "📜 <b>История последних поисков:</b>\n\n"
        for i, entry in enumerate(history, 1):
            text += f"{i}. {html.escape(entry.get('station_name', 'АЗС'))} — {entry['recorded_at'].strftime('%d.%m %H:%M')}\n"
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]]),
            parse_mode="HTML"
        )


@router.callback_query(F.data == "silent_settings")
async def silent_settings(callback: types.CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕐 23:00 – 07:00", callback_data="silent_23_7")],
        [InlineKeyboardButton(text="🕐 00:00 – 06:00", callback_data="silent_0_6")],
        [InlineKeyboardButton(text="❌ Отключить тишину", callback_data="silent_off")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]
    ])
    await callback.message.edit_text("🔇 Выберите тихие часы, когда уведомления не будут приходить:", reply_markup=kb)


@router.callback_query(lambda c: c.data.startswith("silent_"))
async def set_silent(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    async with AsyncSessionLocal() as db:
        if parts[1] == "off":
            await clear_silent_hours(db, callback.from_user.id)
            await callback.message.edit_text("✅ Тишина отключена.")
            return
        start_h, end_h = int(parts[1]), int(parts[2])
        await set_silent_hours(db, callback.from_user.id, start_h, end_h)
    await callback.message.edit_text(f"✅ Тихие часы сохранены: {start_h}:00 – {end_h}:00.")


@router.callback_query(F.data == "referral_hub")
async def referral_hub(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        ref_link = await get_referral_link(db, user)
        text = (
            f"🎁 <b>Приглашай друзей и получай PRO бесплатно</b>\n\n"
            f"За каждого друга, выполнившего первый поиск, вы оба получаете <b>+3 дня PRO</b>!\n\n"
            f"🔗 Ваша ссылка:\n<code>{ref_link}</code>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться", switch_inline_query=f"ref_{user.referral_code}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
