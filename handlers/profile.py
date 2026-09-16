# handlers/profile.py — ПОЛНАЯ ФИНАЛЬНАЯ ВЕРСИЯ (компактная сетка 2-2-2-2/2-2-2-1)
import html
import logging
from datetime import datetime, timezone
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, get_user_search_count, get_user_search_history,
    set_silent_hours, clear_silent_hours, commit_or_rollback,
    get_referral_link
)
from database.models import FuelType
from keyboards.reply import main_menu_keyboard, request_geo_or_city_keyboard
from keyboards.inline import (
    get_fuel_selection_keyboard,
    profile_keyboard
)
from services.subscription import check_pro
from states.city import CitySelectStates

router = Router()
logger = logging.getLogger(__name__)

LEVELS = [
    (0, "🥉 Наблюдатель", 0),
    (5, "🥈 Местный Штурман", 1),
    (20, "🥇 Эксперт Дорог", 3),
    (50, "👑 Топливный Барон", 7),
    (100, "🏆 Легенда Автотрасс", 14),
]


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


# ====================== ОТОБРАЖЕНИЕ ПРОФИЛЯ ======================
@router.message(F.text == "👤 Профиль")
async def show_profile_message(message: types.Message):
    await show_profile(message, user_telegram_id=message.from_user.id)


async def show_profile(message: types.Message, user_telegram_id: int = None):
    """Показ профиля. Для вызовов из callback user_telegram_id обязателен."""
    target_user_id = user_telegram_id if user_telegram_id else message.from_user.id
    logger.info(f"[show_profile] target={target_user_id}, from_message={message.from_user.id}, from_callback={user_telegram_id}")

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

        # Компактная клавиатура профиля (2-2-2-2 или 2-2-2-1)
        kb = profile_keyboard(is_pro=is_pro)

        await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ====================== СМЕНА ГОРОДА (делегировано в start.py) ======================
@router.callback_query(F.data == "change_city")
async def change_city_request(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(CitySelectStates.waiting_city_name)
    await state.update_data(origin="profile")
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "🏙 <b>Смена города</b>\n\n"
        "Нажмите кнопку ниже, отправьте геопозицию через скрепку 📎 (📎 → Геопозиция) "
        "или напишите город текстом (например: <i>Москва</i>, <i>Казань</i>):",
        reply_markup=request_geo_or_city_keyboard(),
        parse_mode="HTML"
    )


# ====================== СМЕНА ТОПЛИВА ======================
@router.callback_query(F.data == "change_fuel")
async def change_fuel(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if user and user.default_fuel:
            default_fuel = getattr(user.default_fuel, "value", "АИ-95")
        else:
            default_fuel = "АИ-95"
        kb = get_fuel_selection_keyboard(default_fuel)
    try:
        await callback.message.edit_text("⛽ Выберите топливо по умолчанию:", reply_markup=kb)
    except Exception:
        await callback.message.answer("⛽ Выберите топливо по умолчанию:", reply_markup=kb)


@router.callback_query(lambda c: c.data.startswith("fuel_"))
async def set_fuel(callback: types.CallbackQuery):
    fuel_type = callback.data.split("_")[1]
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if user:
            try:
                setattr(user, 'default_fuel', FuelType(fuel_type))
            except ValueError:
                mapping = {
                    "АИ-92": FuelType.AI_92, "АИ-95": FuelType.AI_95,
                    "АИ-98": FuelType.AI_98, "АИ-100": FuelType.AI_100, "ДТ": FuelType.DT,
                }
                if fuel_type in mapping:
                    setattr(user, 'default_fuel', mapping[fuel_type])
            await commit_or_rollback(db)
    await callback.answer(f"✅ Топливо {fuel_type} сохранено")
    await show_profile(callback.message, user_telegram_id=callback.from_user.id)


# ====================== СТАТИСТИКА ======================
@router.callback_query(F.data == "stats")
async def show_stats(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.answer("Сначала /start")
            return
        search_count = await get_user_search_count(db, user.id)
        text = (
            f"📊 <b>Ваша статистика</b>\n\n"
            f"🔍 Поисков: {search_count}\n"
            f"💰 Сэкономлено: {user.total_saved or 0:.2f} ₽\n"
            f"⭐ Репутация: {user.reputation}\n"
        )
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]
                ]),
                parse_mode="HTML"
            )
        except Exception:
            await callback.message.answer(text, parse_mode="HTML")


@router.callback_query(F.data == "back_to_profile")
async def back_to_profile(callback: types.CallbackQuery):
    await callback.answer()
    await show_profile(callback.message, user_telegram_id=callback.from_user.id)


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard())


# ====================== ИСТОРИЯ ПОИСКОВ ======================
@router.callback_query(F.data == "search_history")
async def show_search_history(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.message.answer("Сначала /start")
            return
        history = await get_user_search_history(db, user.id, limit=10)
        if not history:
            try:
                await callback.message.edit_text(
                    "📜 История пока пуста.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]
                    ])
                )
            except Exception:
                await callback.message.answer("📜 История пока пуста.")
            return
        text = "📜 <b>История последних поисков:</b>\n\n"
        for i, entry in enumerate(history, 1):
            text += f"{i}. {html.escape(entry.get('station_name', 'АЗС'))} — {entry['recorded_at'].strftime('%d.%m %H:%M')}\n"
        try:
            await callback.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]
                ]),
                parse_mode="HTML"
            )
        except Exception:
            await callback.message.answer(text, parse_mode="HTML")


# ====================== ТИХИЕ ЧАСЫ ======================
@router.callback_query(F.data == "silent_settings")
async def silent_settings(callback: types.CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🕐 23:00 – 07:00", callback_data="silent_23_7"),
         InlineKeyboardButton(text="🕐 00:00 – 06:00", callback_data="silent_0_6")],
        [InlineKeyboardButton(text="❌ Отключить тишину", callback_data="silent_off")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]
    ])
    try:
        await callback.message.edit_text(
            "🔇 Выберите тихие часы, когда уведомления не будут приходить:",
            reply_markup=kb
        )
    except Exception:
        await callback.message.answer("🔇 Настройка тишины:", reply_markup=kb)


@router.callback_query(lambda c: c.data.startswith("silent_"))
async def set_silent(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    async with AsyncSessionLocal() as db:
        if parts[1] == "off":
            await clear_silent_hours(db, callback.from_user.id)
            try:
                await callback.message.edit_text("✅ Тишина отключена.")
            except Exception:
                await callback.message.answer("✅ Тишина отключена.")
            return
        try:
            start_h, end_h = int(parts[1]), int(parts[2])
        except (ValueError, IndexError):
            await callback.message.answer("Ошибка формата.")
            return
        await set_silent_hours(db, callback.from_user.id, start_h, end_h)
    try:
        await callback.message.edit_text(f"✅ Тихие часы сохранены: {start_h}:00 – {end_h}:00.")
    except Exception:
        await callback.message.answer(f"✅ Тихие часы: {start_h}:00 – {end_h}:00.")


# ====================== РЕФЕРАЛЬНЫЙ ХАБ ======================
@router.callback_query(F.data == "referral_hub")
async def referral_hub(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.message.answer("Сначала /start")
            return

        ref_link = await get_referral_link(db, user)
        if not ref_link:
            bot_username = (await callback.bot.get_me()).username
            ref_link = f"https://t.me/{bot_username}?start=ref_{user.referral_code}"

        text = (
            f"🎁 <b>Приглашай друзей и получай PRO бесплатно</b>\n\n"
            f"За каждого друга, выполнившего первый поиск, вы оба получаете <b>+3 дня PRO</b>!\n\n"
            f"🔗 Ваша ссылка:\n<code>{ref_link}</code>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться", switch_inline_query=f"ref_{user.referral_code}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]
        ])
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
