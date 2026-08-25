from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta, timezone

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, get_city_by_name, is_user_pro, get_user_achievements,
    get_referral_link, get_user_search_count, get_user_referrals_count,
    get_next_achievement_progress, get_missed_price_drops,
    get_potential_saving
)
from database.models import FuelType
from keyboards.reply import main_menu_keyboard
from keyboards.inline import popular_cities_keyboard, pro_purchase_keyboard
from services.subscription import format_pro_until, check_pro

router = Router()

LEVELS = [
    (0, "Новичок", 0),
    (10, "Знаток", 1),
    (50, "Эксперт", 3),
    (100, "Легенда", 7),
]

def get_user_level(reputation: int) -> tuple:
    for i, (threshold, name, bonus) in enumerate(LEVELS):
        if reputation < threshold:
            next_threshold = LEVELS[i][0] if i < len(LEVELS) else None
            next_name = LEVELS[i][1] if i < len(LEVELS) else None
            return name, bonus, next_threshold - reputation if next_threshold else 0, next_name
    return LEVELS[-1][1], LEVELS[-1][2], 0, None

@router.message(F.text == "👤 Профиль")
async def show_profile(message: types.Message):
    user_id = message.from_user.id
    async with AsyncSessionLocal() as db:
        user = await get_user(db, user_id)
        if not user:
            await message.answer("Сначала выполните /start")
            return

        city_name = user.city.name if user.city else "Не задан"
        fuel_display = user.default_fuel.value if hasattr(user.default_fuel, 'value') else str(user.default_fuel)
        tank_volume = user.tank_volume

        is_pro = await check_pro(user.telegram_id)
        pro_status = f"✅ Активен до {format_pro_until(user.pro_until)}" if is_pro and user.pro_until else "❌ Не активен"
        next_payment = user.pro_until.strftime("%d.%m.%Y") if user.pro_until else None

        reputation = user.reputation or 0
        level_name, level_bonus, rep_to_next, next_level_name = get_user_level(reputation)
        if rep_to_next > 0:
            level_text = f"Уровень: **{level_name}** (репутация {reputation}/{reputation + rep_to_next})"
            level_text += f"\n   До следующего уровня ({next_level_name}) осталось {rep_to_next} баллов"
        else:
            level_text = f"Уровень: **{level_name}** (репутация {reputation})"

        # Бонус уровня
        if level_bonus > 0:
            level_text += f"\n   → Бонус уровня: +{level_bonus} дн PRO при активации"

        # Достижения
        achievements = await get_user_achievements(db, user.id)
        total_achievements = len(achievements)
        next_ach = await get_next_achievement_progress(db, user.id)

        # Рефералы
        referrals_count = await get_user_referrals_count(db, user.id)
        bonus_days_from_achievements = sum(a.bonus_days_granted for a in achievements)

        # Поиски
        search_count = await get_user_search_count(db, user.id)

        # Потенциальная экономия и пропущенные уведомления
        potential_saving = await get_potential_saving(db, user.id)
        missed_drops = await get_missed_price_drops(db, user.city_id)
        # Средняя экономия за поиск (если есть)
        if search_count > 0 and user.total_saved:
            avg_saving = user.total_saved / search_count
        else:
            avg_saving = 0

        # ---- Формируем текст ----
        text = f"👤 **Ваш профиль BinzoLife**\n\n"
        text += f"📍 Город: {city_name}\n"
        text += f"⛽ Топливо: {fuel_display} (бак {tank_volume} л)\n"
        text += f"🏅 {level_text}\n\n"

        # ---- Финансы ----
        text += "💰 **Финансы**\n"
        if potential_saving > 0:
            text += f"▪ Потенциальная экономия с PRO: **до {potential_saving:.0f} ₽** за последние заправки\n"
        else:
            text += "▪ Потенциальная экономия с PRO: **до 500 ₽** за заправку\n"
        if missed_drops > 0:
            text += f"▪ За последнюю неделю вы могли сэкономить **{missed_drops * 50:.0f} ₽**, если бы получали уведомления о снижении цен\n"
        if avg_saving > 0:
            text += f"▪ Средняя экономия за поиск: {avg_saving:.0f} ₽\n"
        else:
            text += "▪ Средняя экономия за поиск: пока нет данных\n"
        text += "\n"

        # ---- Уведомления (FOMO) ----
        text += "🔔 **Уведомления**\n"
        if missed_drops > 0:
            text += f"▪ За неделю цена на ваших АЗС снижалась **{missed_drops} раз**\n"
            text += f"▪ Вы пропустили экономию до **{missed_drops * 50:.0f} ₽**. С PRO вы бы не упустили.\n"
            text += f"👉 [Подключить PRO →](https://t.me/BinzoLife_bot?start=pro)\n"
        else:
            text += "▪ У вас пока нет пропущенных уведомлений. Но с PRO вы будете первыми узнавать о снижении цен.\n"
        text += "\n"

        # ---- Репутация ----
        text += f"⭐ **Репутация: {reputation}**\n"
        text += "Как заработать:\n"
        text += "• Сообщить цену → +1\n"
        text += "• Привести друга → +3 дня PRO\n"
        text += "• Написать отзыв → +2\n"
        text += "\n"

        # ---- Достижения ----
        text += "🏅 **Достижения**\n"
        if total_achievements > 0:
            text += f"▪ Получено: {total_achievements} наград\n"
        if next_ach:
            ach_name, progress, target = next_ach
            bar_len = 20
            filled = int(progress / target * bar_len) if target > 0 else 0
            bar = "█" * filled + "░" * (bar_len - filled)
            text += f"▪ Ближайшее: «{ach_name}» — "
            if "репорт" in ach_name.lower():
                text += "сообщите 1 цену\n"
            elif "пригласил" in ach_name.lower():
                text += "пригласите 1 друга\n"
            elif "экономия" in ach_name.lower():
                text += f"накопите экономию {target} ₽\n"
            else:
                text += f"прогресс {progress}/{target}\n"
            text += f"   Прогресс: {bar} {progress}/{target}\n"
            if progress < target:
                text += f"   Награда: +1 репутация + бейдж\n"
        else:
            text += "▪ Все достижения получены! Вы — легенда!\n"
        text += "\n"

        # ---- Рефералы ----
        text += "👥 **Рефералы**\n"
        text += f"▪ Приглашено друзей: {referrals_count}\n"
        text += f"▪ Бонусных дней PRO получено: {bonus_days_from_achievements}\n"
        # Реферальная ссылка в тексте
        referral_link = await get_referral_link(db, user)
        text += f"📎 Ваша ссылка: {referral_link}\n"
        text += "\n"

        # ---- PRO-призыв (FOMO + CTA) ----
        if is_pro and next_payment:
            text += f"💳 Следующее списание: {next_payment}\n"
        else:
            text += "💎 **Вы теряете до 500 ₽ на каждой заправке без PRO.**\n"
            text += "Окупится с первой поездки.\n"
            # Кнопка PRO будет в клавиатуре

        # ---- Клавиатура ----
        kb_buttons = [
            [InlineKeyboardButton(text="🏙 Изменить город", callback_data="change_city"),
             InlineKeyboardButton(text="⛽ Изменить топливо", callback_data="change_fuel")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="stats")],
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
    await callback.message.edit_text(
        "⛽ Выберите топливо по умолчанию:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="АИ-92", callback_data="fuel_AI-92")],
            [InlineKeyboardButton(text="АИ-95", callback_data="fuel_AI-95")],
            [InlineKeyboardButton(text="АИ-98", callback_data="fuel_AI-98")],
            [InlineKeyboardButton(text="ДТ", callback_data="fuel_DT")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_profile")]
        ])
    )

@router.callback_query(lambda c: c.data.startswith("fuel_"))
async def set_fuel(callback: types.CallbackQuery):
    fuel_type = callback.data.split("_")[1]
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if user:
            setattr(user, 'default_fuel', FuelType(fuel_type))
            await db.commit()
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
            f"📊 **Ваша статистика**\n\n"
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
