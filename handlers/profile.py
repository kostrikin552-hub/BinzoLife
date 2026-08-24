from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime, timedelta, timezone

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, get_user_achievements, get_referral_link,
    get_user_search_count, get_user_referrals_count,
    get_missed_price_drops, get_potential_saving,
    get_next_achievement_progress
)
from database.models import User
from keyboards.reply import main_menu_keyboard
from keyboards.inline import popular_cities_keyboard
from services.subscription import format_pro_until, check_pro
from config import settings

router = Router()

# ---------- Уровни на основе репутации ----------
LEVELS = [
    (0, "Новичок", 0),
    (10, "Знаток", 1),
    (50, "Эксперт", 3),
    (100, "Легенда", 7),
]

def get_user_level(reputation: int) -> tuple:
    """Возвращает (название уровня, бонус_дней, репутация_до_следующего_уровня)"""
    for i, (threshold, name, bonus) in enumerate(LEVELS):
        if reputation < threshold:
            next_threshold = LEVELS[i][0] if i < len(LEVELS) else None
            return name, bonus, next_threshold - reputation if next_threshold else 0
    # Если репутация >= последнего порога
    return LEVELS[-1][1], LEVELS[-1][2], 0

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

        # PRO статус
        is_pro = await check_pro(user.telegram_id)
        pro_status = f"✅ Активен до {format_pro_until(user.pro_until)}" if is_pro and user.pro_until else "❌ Не активен"
        next_payment = user.pro_until.strftime("%d.%m.%Y") if user.pro_until else None

        # Репутация и уровень
        reputation = user.reputation or 0
        level_name, level_bonus, rep_to_next = get_user_level(reputation)

        # Достижения
        achievements = await get_user_achievements(db, user.id)
        total_achievements = len(achievements)
        # Следующее достижение (прогресс)
        next_ach = await get_next_achievement_progress(db, user.id)

        # Статистика поисков
        search_count = await get_user_search_count(db, user.id)

        # Рефералы
        referrals_count = await get_user_referrals_count(db, user.id)
        referral_days_earned = 0
        # Бонусные дни из достижений
        bonus_days_from_achievements = sum(a.bonus_days_granted for a in achievements)

        # Экономия
        total_saved = user.total_saved or 0.0
        # Потенциальная экономия с PRO (за последние 30 дней)
        potential_saving = await get_potential_saving(db, user.id)

        # Пропущенные уведомления (количество снижений цены, которые пользователь пропустил)
        missed_drops = await get_missed_price_drops(db, user.city_id)

        # Формируем текст
        text = f"👤 <b>Ваш профиль BinzoLife</b>\n\n"
        text += f"📍 Город: {city_name}\n"
        text += f"⛽ Топливо: {fuel_display} (бак {tank_volume} л)\n"
        text += f"🏅 Уровень: <b>{level_name}</b> (репутация {reputation}"
        if rep_to_next > 0:
            text += f", до следующего уровня {rep_to_next} баллов)"
        else:
            text += ")"
        text += "\n\n"

        text += "💰 <b>Финансы</b>\n"
        text += f"▪ Всего сэкономлено: <b>{total_saved:.2f} ₽</b>\n"
        if search_count > 0:
            avg_saving = total_saved / search_count if search_count > 0 else 0
            text += f"▪ Средняя экономия за поиск: <b>{avg_saving:.2f} ₽</b>\n"
        if potential_saving > 0:
            text += f"▪ С PRO вы бы сэкономили <b>до {potential_saving:.2f} ₽</b> за последние заправки\n"
        else:
            text += "▪ С PRO вы будете получать уведомления о снижении цен\n"
        text += "\n"

        text += "🔔 <b>Уведомления</b>\n"
        if missed_drops > 0:
            text += f"▪ Вы пропустили <b>{missed_drops} снижений цен</b> на ваших АЗС за последнюю неделю\n"
            text += "▪ [Подключить PRO →](https://t.me/BinzoLife_bot?start=pro) — чтобы не упускать выгоду\n"
        else:
            text += "▪ Нет пропущенных уведомлений. Вы в курсе всех выгодных цен!\n"
        text += "\n"

        text += "⭐ <b>Репутация: {reputation}</b>\n".format(reputation=reputation)
        text += "Как заработать:\n"
        text += "• Сообщить цену → +1\n"
        text += "• Привести друга → +3 дня PRO\n"
        text += "• Написать отзыв → +2\n"
        text += "\n"

        text += "🏅 <b>Достижения</b>\n"
        if total_achievements > 0:
            text += f"▪ Получено: {total_achievements} наград\n"
        else:
            text += "▪ У вас пока нет достижений\n"
        if next_ach:
            ach_name, progress, target = next_ach
            bar_len = 20
            filled = int(progress / target * bar_len) if target > 0 else 0
            bar = "█" * filled + "░" * (bar_len - filled)
            text += f"▪ Следующая награда: «{ach_name}»\n"
            text += f"   Прогресс: {bar} {progress}/{target}\n"
        else:
            text += "▪ Все достижения получены! Вы — легенда!\n"
        text += "\n"

        text += "👥 <b>Рефералы</b>\n"
        text += f"▪ Приглашено друзей: {referrals_count}\n"
        text += f"▪ Бонусных дней PRO получено: {bonus_days_from_achievements}\n"
        text += "📎 [Получить реферальную ссылку](command:referral)\n"
        text += "\n"

        if is_pro and next_payment:
            text += f"💳 Следующее списание: {next_payment}\n"
        elif not is_pro:
            text += "💎 <b>Оформите PRO</b> — и получайте экстренные поиски и уведомления без ограничений!\n"

        # Клавиатура
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏙 Изменить город", callback_data="change_city"),
             InlineKeyboardButton(text="⛽ Изменить топливо", callback_data="change_fuel")],
            [InlineKeyboardButton(text="🔗 Получить реферальную ссылку", callback_data="referral")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="stats")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]
        ])

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
            from database.models import FuelType
            setattr(user, 'default_fuel', FuelType(fuel_type))
            await db.commit()
    await callback.answer(f"✅ Топливо {fuel_type} сохранено")
    await show_profile(callback.message)

@router.callback_query(F.data == "referral")
async def referral_link(callback: types.CallbackQuery):
    await callback.answer()
    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.answer("Сначала /start")
            return
        link = await get_referral_link(db, user)
        await callback.message.edit_text(
            f"🔗 Ваша реферальная ссылка:\n{link}\n\n"
            "Пригласите друга, и вы оба получите +3 дня PRO (если друг совершит хотя бы один поиск).\n\n"
            "Активных рефералов: {count}\n"
            "Бонусных дней получено: {days}".format(
                count=await get_user_referrals_count(db, user.id),
                days=sum(a.bonus_days_granted for a in await get_user_achievements(db, user.id))
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_to_profile")]
            ])
        )

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
