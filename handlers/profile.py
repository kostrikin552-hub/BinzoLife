# handlers/profile.py — ПОЛНАЯ ВЕРСИЯ (все изменения)
import html
import logging
from datetime import datetime, timedelta, timezone, date
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, get_city_by_name, is_user_pro, get_user_achievements,
    get_referral_link, get_user_search_count, get_user_referrals_count,
    get_next_achievement_progress, get_missed_price_drops,
    get_potential_saving, get_user_search_history,
    set_silent_hours, clear_silent_hours, is_silent_hours_now,
    set_user_timezone, save_user_location, find_nearest_city,
    commit_or_rollback
)
from database.models import FuelType
from keyboards.reply import main_menu_keyboard
from keyboards.inline import popular_cities_keyboard, pro_purchase_keyboard, get_fuel_selection_keyboard
from services.subscription import format_pro_until, check_pro

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


@router.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message):
    user_id = message.from_user.id
    async with AsyncSessionLocal() as db:
        user = await get_user(db, user_id)
        if not user:
            await message.answer("Сначала выполните /start")
            return

        city_name = html.escape(user.city.name) if user.city else "Не задан"
        fuel_display = user.default_fuel.value if hasattr(user.default_fuel, 'value') else str(user.default_fuel)
        tank_volume = user.tank_volume

        is_pro = await check_pro(user.telegram_id)
        is_trial = user.trial_used and user.pro_until and user.pro_until > datetime.now(timezone.utc)
        if is_pro and user.pro_until:
            days_left = (user.pro_until - datetime.now(timezone.utc)).days
            status_text = f"✅ Активен до {user.pro_until.strftime('%d.%m.%Y %H:%M')} (осталось {days_left} дн.)"
            if user.trial_used:
                status_text = "🎁 Пробный период " + status_text
        elif is_trial:
            days_left = (user.pro_until - datetime.now(timezone.utc)).days
            status_text = f"🎁 Пробный период активен до {user.pro_until.strftime('%d.%m.%Y %H:%M')} (осталось {days_left} дн.)"
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

        achievements = await get_user_achievements(db, user.id)
        total_achievements = len(achievements)
        next_ach = await get_next_achievement_progress(db, user.id)

        referrals_count = await get_user_referrals_count(db, user.id)
        bonus_days_from_achievements = sum(a.bonus_days_granted for a in achievements)

        search_count = await get_user_search_count(db, user.id)
        potential_saving = await get_potential_saving(db, user.id)
        missed_drops = await get_missed_price_drops(db, user.city_id)
        avg_saving = user.total_saved / search_count if search_count > 0 else 0

        # Прогресс рефералов
        ref_count = user.invited_count or 0
        if ref_count < 1:
            ref_goal = "Пригласите 1 друга и получите +3 дня PRO!"
        elif ref_count < 3:
            ref_goal = f"Приглашено: {ref_count}/3. Ещё {3 - ref_count} друга до бонуса +14 дней PRO!"
        elif ref_count < 10:
            ref_goal = f"Приглашено: {ref_count}/10. Ещё {10 - ref_count} до Вечного PRO!"
        else:
            ref_goal = "👑 Вы — Амбассадор BinzoLife! У вас вечный PRO-доступ."

        text = (
            f"👤 <b>Кабинет водителя</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 <b>Сохранено в кошельке:</b> <code>{user.total_saved or 0:,.0f} ₽</code>\n"
            f"{level_line}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ <b>Параметры авто:</b>\n"
            f"• Город: <b>{city_name}</b>\n"
            f"• Топливо: <b>{fuel_display}</b>\n"
            f"• Объём бака: <b>{tank_volume} л</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💎 <b>Статус аккаунта:</b> {status_text}\n"
            f"🏅 <b>Достижений:</b> {total_achievements}\n"
            f"👥 <b>Рефералов:</b> {ref_count} ({ref_goal})\n"
            f"🔍 <b>Поисков:</b> {search_count}\n"
            f"📊 <b>Средняя экономия:</b> {avg_saving:.0f} ₽/поиск\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏳ <b>Тихие часы:</b> "
        )
        if user.silent_hours_start is not None and user.silent_hours_end is not None:
            text += f"{user.silent_hours_start}:00 – {user.silent_hours_end}:00"
        else:
            text += "не настроены"

        # Клавиатура
        kb_buttons = [
            [InlineKeyboardButton(text="🏙 Изменить город", callback_data="change_city"),
             InlineKeyboardButton(text="⛽ Изменить топливо", callback_data="change_fuel")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="stats")],
            [InlineKeyboardButton(text="📜 История поисков", callback_data="search_history")],
            [InlineKeyboardButton(text="🔇 Настройка тишины", callback_data="silent_settings")],
            [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="referral_hub")],
        ]
        if not is_pro:
            kb_buttons.append([InlineKeyboardButton(text="🔥 Оформить PRO за 99 ₽", callback_data="buy_pro")])
        kb_buttons.append([InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")])

        kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

        await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ---------- Обработчики кнопок ----------
@router.callback_query(F.data == "change_city")
async def change_city(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.edit_text(
        "📍 Выбери новый город из списка:",
        reply_markup=popular_cities_keyboard(with_back=True)
    )


@router.callback_query(F.data == "change_fuel")
async def change_fuel(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        default_fuel = user.default_fuel.value if user else "АИ-95"
        kb = get_fuel_selection_keyboard(default_fuel)
    await callback.message.edit_text(
        "⛽ Выберите топливо по умолчанию:",
        reply_markup=kb
    )


@router.callback_query(lambda c: c.data.startswith("fuel_"))
async def set_fuel(callback: types.CallbackQuery):
    fuel_type = callback.data.split("_")[1]
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if user:
            setattr(user, 'default_fuel', FuelType(fuel_type))
            await commit_or_rollback(db)
    await callback.answer(f"✅ Топливо {fuel_type} сохранено")
    await show_profile(callback.message)


@router.callback_query(F.data == "stats")
async def show_stats(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.answer("Сначала /start")
            return
        search_count = await get_user_search_count(db, user.id)
        economy_count = user.total_saved or 0
        text = (
            f"📊 <b>Ваша статистика</b>\n\n"
            f"🔍 Поисков заправок: {search_count}\n"
            f"💰 Всего сэкономлено: {economy_count:.2f} ₽\n"
            f"⭐ Репутация: {user.reputation}\n"
            f"🏅 Достижений: {len(await get_user_achievements(db, user.id))}\n"
            f"👥 Приглашено друзей: {await get_user_referrals_count(db, user.id)}"
        )
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_to_profile")]
            ]),
            parse_mode="HTML"
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


# ---------- История поисков ----------
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
            await callback.message.edit_text(
                "📜 У вас пока нет истории поисков.\n"
                "Начните искать заправки, и они появятся здесь.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_to_profile")]
                ])
            )
            return
        text = "📜 <b>История ваших поисков</b>\n\n"
        for i, entry in enumerate(history, 1):
            station_name = html.escape(entry.get("station_name", "неизвестно"))
            time_str = entry["recorded_at"].strftime("%d.%m %H:%M")
            text += f"{i}. {station_name} — {time_str}\n"
        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_to_profile")]
            ]),
            parse_mode="HTML"
        )


# ---------- Настройка тишины ----------
@router.callback_query(F.data == "silent_settings")
async def silent_settings(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.message.answer("Сначала /start")
            return
        current_start = user.silent_hours_start
        current_end = user.silent_hours_end
        status = f"🔇 Текущие настройки: {current_start}:00 – {current_end}:00" if current_start is not None else "🔇 Тишина не настроена"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🕐 23:00 – 07:00", callback_data="silent_23_7")],
            [InlineKeyboardButton(text="🕐 00:00 – 06:00", callback_data="silent_0_6")],
            [InlineKeyboardButton(text="🕐 22:00 – 08:00", callback_data="silent_22_8")],
            [InlineKeyboardButton(text="❌ Отключить тишину", callback_data="silent_off")],
            [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_to_profile")]
        ])
        await callback.message.edit_text(
            f"{status}\n\nВыберите интервал, когда уведомления не должны приходить:",
            reply_markup=kb
        )


@router.callback_query(lambda c: c.data.startswith("silent_"))
async def set_silent(callback: types.CallbackQuery):
    await callback.answer()
    parts = callback.data.split("_")
    if parts[1] == "off":
        async with AsyncSessionLocal() as db:
            await clear_silent_hours(db, callback.from_user.id)
        await callback.message.edit_text("✅ Тишина отключена. Уведомления будут приходить в любое время.")
        return
    start_hour, end_hour = map(int, parts[1].split("_"))
    async with AsyncSessionLocal() as db:
        await set_silent_hours(db, callback.from_user.id, start_hour, end_hour)
    await callback.message.edit_text(f"✅ Тишина настроена: {start_hour}:00 – {end_hour}:00. Уведомления не будут приходить в этот период.")


# ---------- Реферальный хаб ----------
@router.callback_query(F.data == "referral_hub")
async def referral_hub(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.message.answer("Сначала /start")
            return

        ref_link = await get_referral_link(db, user)
        ref_count = user.invited_count or 0

        if ref_count < 1:
            next_goal = "Пригласите 1 друга и получите +3 дня PRO!"
        elif ref_count < 3:
            next_goal = f"Приглашено: {ref_count}/3. Ещё {3 - ref_count} друга до бонуса +14 дней PRO!"
        elif ref_count < 10:
            next_goal = f"Приглашено: {ref_count}/10. Ещё {10 - ref_count} до Вечного PRO!"
        else:
            next_goal = "👑 Вы — Амбассадор BinzoLife! У вас вечный PRO-доступ."

        bot_username = (await callback.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user.referral_code}"

        text = (
            f"🎁 <b>Топливный клуб BinzoLife: Приглашай и не плати за PRO</b>\n\n"
            f"Поделитесь ссылкой с коллегами и друзьями-автомобилистами. "
            f"Когда друг сделает свой первый поиск, вы оба получите бонусы!\n\n"
            f"📊 <b>Ваша статистика:</b>\n"
            f"• Приглашено друзей: <b>{ref_count}</b>\n"
            f"• Следующая цель: <i>{next_goal}</i>\n\n"
            f"🔗 <b>Ваша персональная ссылка:</b>\n"
            f"<code>{ref_link}</code>\n\n"
            f"<i>(Нажмите на ссылку, чтобы скопировать её)</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", switch_inline_query=f"ref_{user.referral_code}")],
            [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_to_profile")]
        ])
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
