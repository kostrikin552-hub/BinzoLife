from aiogram import types
from aiogram.dispatcher import Dispatcher
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from database.crud import (
    get_user, create_user, get_all_active_cities, get_city_by_name,
    update_user, apply_referral, set_first_search, log_action
)
from keyboards.main_menu import get_main_menu, get_cities_keyboard
from keyboards.inline import get_back_to_menu_button
import logging

async def cmd_start(message: types.Message, session, state: FSMContext):
    await state.clear()
    user = await get_user(session, message.from_user.id)
    if not user:
        ref = None
        if message.text and "ref_" in message.text:
            ref = message.text.split("ref_")[1]
        user = await create_user(session, message.from_user.id, message.from_user.username, ref)
        if ref:
            await apply_referral(session, user.id, ref)
    
    # Проверка подписки на канал (опционально) — здесь может быть ваш код
    
    cities = await get_all_active_cities(session)
    city_names = [c.name for c in cities]
    
    # Если у пользователя уже есть город — сразу показываем меню
    if user.city_id:
        city = await get_city_by_name(session, user.city.name)
        await message.answer(
            f"⛽ Добро пожаловать в BinzoLife!\n\n"
            f"Ваш город: {city.name if city else 'не выбран'}.\n"
            f"Что делаем?",
            reply_markup=get_main_menu()
        )
        return
    
    await message.answer(
        "⛽ Добро пожаловать в BinzoLife!\n\n"
        "Я — ваш персональный помощник по поиску самой выгодной цены на топливо.\n"
        "Больше не нужно гадать, где дешевле — я покажу актуальные цены и наличие на АЗС в вашем городе.\n\n"
        "🚀 Что вы получите:\n"
        "• Мгновенный поиск лучшей цены на АИ‑95\n"
        "• Информацию о наличии топлива (зелёный/жёлтый/красный)\n"
        "• Карточку АЗС с адресом, расстоянием и маршрутом\n"
        "• Возможность сообщать цены и получать репутацию\n\n"
        "💎 PRO-подписка (99 ₽ / 150 Stars) даст вам:\n"
        "• Уведомления о снижении цены на конкретной АЗС\n"
        "• График изменения цен за 30 дней\n"
        "• Безлимитный поиск\n"
        "• Приоритетные уведомления\n\n"
        "🎁 Прямо сейчас у вас есть 3 дня PRO бесплатно — активируются после первого поиска.\n\n"
        "👉 Начните с выбора города:",
        reply_markup=get_cities_keyboard(city_names)
    )

async def city_selected(message: types.Message, session):
    city_name = message.text
    user = await get_user(session, message.from_user.id)
    if not user:
        await message.answer("Ошибка: пользователь не найден. Напишите /start")
        return
    
    city = await get_city_by_name(session, city_name)
    if not city:
        await message.answer(f"Город {city_name} не найден. Выберите из списка.")
        return
    
    user.city_id = city.id
    await update_user(session, user)
    await log_action(session, user.id, "city_selected")
    
    await message.answer(
        f"✅ Город {city_name} сохранён. Теперь я буду показывать цены именно для этого города.\n\n"
        f"Что делаем?",
        reply_markup=get_main_menu()
    )

def register_handlers(dp: Dispatcher):
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(city_selected, lambda msg: msg.text in [c.name for c in get_all_active_cities()])
