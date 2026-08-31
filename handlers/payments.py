import json
import logging
from aiogram import Router, types, F
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment, InlineKeyboardMarkup, InlineKeyboardButton
from config import settings
from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_user, create_payment, activate_pro, get_payment_by_telegram_charge_id,
    is_user_pro, get_city_by_name, update_user, grant_emergency_search,
    activate_trial
)
from services.subscription import format_pro_until
from keyboards.reply import main_menu_keyboard
from keyboards.inline import pro_purchase_keyboard
from datetime import datetime, timedelta, timezone

router = Router()
logger = logging.getLogger(__name__)

# ===== ТАРИФНАЯ СЕТКА =====
TARIFFS = {
    "pro_24h": {
        "title": "⚡ PRO на 24 часа (Поездка)",
        "price_rub": 29,
        "price_stars": 15,
        "days": 1,
        "badge": "Для быстрой заправки"
    },
    "pro_1m": {
        "title": "👑 PRO 1 месяц (Хит)",
        "price_rub": 99,
        "price_stars": 50,
        "days": 30,
        "badge": "Выгода 70%"
    },
    "pro_3m": {
        "title": "🔥 PRO 3 месяца (Сезон)",
        "price_rub": 249,
        "price_stars": 130,
        "days": 90,
        "badge": "Максимальная экономия"
    }
}
# =================================

async def send_invoice(message: types.Message, amount: int, payload: str, description: str, currency: str = "RUB"):
    prices = [LabeledPrice(label=description, amount=amount * 100 if currency == "RUB" else amount)]
    try:
        await message.answer_invoice(
            title=f"Оплата {amount} {currency}",
            description=description,
            provider_token=settings.PROVIDER_TOKEN if currency == "RUB" else "",
            currency=currency,
            prices=prices,
            start_parameter="payment",
            payload=payload,
            provider_data=json.dumps({"receipt": {"items": [{"description": description, "quantity": "1", "amount": {"value": str(amount), "currency": currency}}]}}) if currency == "RUB" else None
        )
    except Exception as e:
        logger.error(f"Ошибка отправки инвойса: {e}")
        await message.answer("❌ Не удалось создать платёж. Попробуйте позже.")

@router.message(F.text == "💎 PRO")
async def show_pro_info(message: types.Message):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала выполните /start")
            return
        pro_active = await is_user_pro(db, user)
        if pro_active and user.pro_until:
            until = format_pro_until(user.pro_until)
            auto_renew = "включено" if user.auto_renew else "выключено"
            await message.answer(
                f"✅ Ваш PRO активен до {until}\n"
                f"🔄 Автопродление: {auto_renew}\n\n"
                "Спасибо, что поддерживаете проект!",
                reply_markup=main_menu_keyboard()
            )
            return

    # Показываем тарифы
    text = "💎 **BinzoLife PRO — экономь на каждой заправке**\n\n"
    text += "Выбери подходящий тариф:\n\n"
    for key, tariff in TARIFFS.items():
        text += f"**{tariff['title']}**\n"
        text += f"💰 {tariff['price_rub']} ₽ / {tariff['price_stars']} Stars\n"
        text += f"📅 {tariff['days']} дней\n"
        text += f"🏷 {tariff['badge']}\n\n"
    text += "👇 Нажми на кнопку тарифа, чтобы оплатить."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ 29 ₽ / 24ч", callback_data="buy_tariff_pro_24h"),
         InlineKeyboardButton(text="👑 99 ₽ / мес", callback_data="buy_tariff_pro_1m")],
        [InlineKeyboardButton(text="🔥 249 ₽ / 3 мес", callback_data="buy_tariff_pro_3m")],
        [InlineKeyboardButton(text="⭐ Оплатить Stars", callback_data="buy_pro_stars")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@router.callback_query(lambda c: c.data.startswith("buy_tariff_"))
async def buy_tariff(callback: types.CallbackQuery):
    tariff_id = callback.data.split("_")[2] + "_" + callback.data.split("_")[3]  # pro_24h, pro_1m, pro_3m
    tariff = TARIFFS.get(tariff_id)
    if not tariff:
        await callback.answer("Тариф не найден")
        return

    prices = [LabeledPrice(label=tariff["title"], amount=tariff["price_rub"] * 100)]
    try:
        await callback.message.answer_invoice(
            title=tariff["title"],
            description=f"PRO на {tariff['days']} дней. {tariff['badge']}",
            provider_token=settings.PROVIDER_TOKEN,
            currency="RUB",
            prices=prices,
            start_parameter="pro_subscription",
            payload=f"pro_{tariff_id}"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"Ошибка создания инвойса: {e}")
        await callback.message.answer("❌ Не удалось создать платёж")

# ===== ОБРАБОТЧИК ДЛЯ СТАРЫХ КНОПОК "Купить PRO" =====
@router.callback_query(F.data == "buy_pro")
async def buy_pro_legacy(callback: types.CallbackQuery):
    """Обработчик старых кнопок 'Купить PRO' — перенаправляет на новые тарифы."""
    await callback.answer()
    await show_pro_info(callback.message)

@router.callback_query(F.data == "buy_pro_stars")
async def buy_pro_stars(callback: types.CallbackQuery):
    await callback.answer()
    tariff = TARIFFS["pro_1m"]
    prices = [LabeledPrice(label=tariff["title"], amount=tariff["price_stars"])]
    try:
        await callback.message.answer_invoice(
            title="💎 PRO 1 месяц (Stars)",
            description="PRO на 30 дней. Оплата звёздами.",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="pro_stars",
            payload="pro_month_stars"
        )
    except Exception as e:
        logger.error(f"Ошибка Stars инвойса: {e}")
        await callback.message.answer("❌ Не удалось создать платёж через Stars. Попробуйте рублёвую оплату.")

@router.pre_checkout_query()
async def pre_checkout_query(pre_checkout: PreCheckoutQuery):
    payload = pre_checkout.invoice_payload
    if not payload:
        await pre_checkout.answer(ok=False, error_message="Некорректный платёж")
        return

    all_ok = False
    if payload.startswith("pro_"):
        tariff_id = payload.split("_")[1] + "_" + payload.split("_")[2]
        tariff = TARIFFS.get(tariff_id)
        if tariff and pre_checkout.total_amount == tariff["price_rub"] * 100 and pre_checkout.currency == "RUB":
            all_ok = True
    elif payload == "pro_month_stars":
        tariff = TARIFFS["pro_1m"]
        if pre_checkout.total_amount == tariff["price_stars"] and pre_checkout.currency == "XTR":
            all_ok = True
    elif payload == "emergency_search_rub":
        if pre_checkout.total_amount == 50 * 100 and pre_checkout.currency == "RUB":
            all_ok = True
    elif payload == "emergency_search_stars":
        if pre_checkout.total_amount == 50 and pre_checkout.currency == "XTR":
            all_ok = True
    elif payload == "test_payment_payload":
        if pre_checkout.currency == "RUB":
            all_ok = True

    if not all_ok:
        await pre_checkout.answer(ok=False, error_message="Платёж не прошёл. Попробуйте другой способ.")
        # Отправляем сообщение через бота
        await pre_checkout.bot.send_message(
            chat_id=pre_checkout.from_user.id,
            text="❌ Платёж был отменён или не прошёл. Если у вас возникли вопросы, обратитесь в поддержку: @BinzoLife_Support"
        )
        return
    await pre_checkout.answer(ok=True)

@router.callback_query(F.data == "pro_go_find")
async def pro_go_find(callback: types.CallbackQuery):
    await callback.answer()
    from handlers.find import start_find
    await start_find(callback.message, None)

@router.callback_query(F.data == "pro_go_notifications")
async def pro_go_notifications(callback: types.CallbackQuery):
    await callback.answer()
    from handlers.notifications import list_notifications
    await list_notifications(callback.message)

@router.callback_query(F.data == "pro_go_profile")
async def pro_go_profile(callback: types.CallbackQuery):
    await callback.answer()
    from handlers.profile import show_profile
    await show_profile(callback.message)

async def send_pro_activated_message(message: types.Message, until: str, auto_renew: bool, is_test: bool = False):
    if is_test:
        header = "🧪 Тестовый режим — поздравляю! Вы в клубе PRO"
        footer = "Это тестовый платёж, все функции активны для проверки."
    else:
        header = "✅ Поздравляю! Теперь вы в клубе PRO"
        footer = ""

    auto_text = "включено" if auto_renew else "выключено"
    text = (
        f"{header}\n\n"
        "Вы только что сделали шаг к экономии до 500 ₽ на каждой заправке.\n"
        "Уведомления о выгодных ценах и появлении топлива уже активированы — вы не пропустите ни одной выгоды.\n\n"
        "🔔 Ваша первая задача: проверьте, настроены ли уведомления на ваши любимые АЗС.\n"
        "Нажмите кнопку ниже, чтобы сделать это за 30 секунд.\n\n"
        "🚗 А если бензин заканчивается прямо сейчас — нажмите «Найти заправку», и я покажу ближайшую АЗС с топливом.\n\n"
        f"Ваш PRO активен до {until}.\n"
        f"Автопродление: {auto_text} — вы можете отключить его в любой момент в профиле.\n\n"
        f"{footer}\n"
        "Спасибо, что выбрали BinzoLife. Поехали экономить! 💪"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚙️ Настроить уведомления", callback_data="pro_go_notifications")],
        [InlineKeyboardButton(text="🚗 Найти заправку", callback_data="pro_go_find")],
        [InlineKeyboardButton(text="👤 Профиль (управление подпиской)", callback_data="pro_go_profile")]
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payment: SuccessfulPayment = message.successful_payment
    telegram_charge_id = payment.telegram_payment_charge_id
    provider_charge_id = payment.provider_payment_charge_id
    total_amount = payment.total_amount / 100 if payment.currency == "RUB" else payment.total_amount
    payload = payment.invoice_payload
    currency = payment.currency

    logger.info(f"Получен успешный платёж: payload={payload}, currency={currency}, amount={total_amount}")

    if payload == "test_payment_payload":
        if message.from_user.id not in settings.admin_ids:
            await message.answer("⛔ Тестовый платёж доступен только администраторам.")
            return
        async with AsyncSessionLocal() as db:
            async with db.begin():
                user = await get_user(db, message.from_user.id)
                if not user:
                    user = await create_user(db, message.from_user.id, message.from_user.username)
                    city = await get_city_by_name(db, "Красноярск")
                    if city:
                        user.city_id = city.id
                        await db.flush()
                existing = await get_payment_by_telegram_charge_id(db, telegram_charge_id)
                if existing:
                    await message.answer("Этот платёж уже был обработан.")
                    return
                await create_payment(db, user.id, telegram_charge_id, provider_charge_id, total_amount, currency=currency, tariff="test")
                await activate_pro(db, user, days=30)
            until = format_pro_until(user.pro_until)
            await send_pro_activated_message(message, until, auto_renew=False, is_test=True)
            return

    if currency == "XTR" and payload == "emergency_search_stars":
        async with AsyncSessionLocal() as db:
            user = await get_user(db, message.from_user.id)
            if not user:
                await message.answer("Сначала /start")
                return
            await grant_emergency_search(db, user.id)
        await message.answer(
            "⭐ Оплата Stars принята! Вы можете использовать экстренный поиск.\n"
            "Нажмите «🚨 Бензин заканчивается!» и введите адрес.",
            reply_markup=main_menu_keyboard()
        )
        return

    if currency == "XTR" and payload == "pro_month_stars":
        tariff = TARIFFS["pro_1m"]
        if total_amount != tariff["price_stars"]:
            await message.answer("❌ Некорректное количество Stars.")
            return
        async with AsyncSessionLocal() as db:
            async with db.begin():
                user = await get_user(db, message.from_user.id)
                if not user:
                    user = await create_user(db, message.from_user.id, message.from_user.username)
                    city = await get_city_by_name(db, "Красноярск")
                    if city:
                        user.city_id = city.id
                        await db.flush()
                existing = await get_payment_by_telegram_charge_id(db, telegram_charge_id)
                if existing:
                    await message.answer("Этот платёж уже был обработан.")
                    return
                await create_payment(db, user.id, telegram_charge_id, provider_charge_id, total_amount, currency="XTR", tariff="pro_month")
                now = datetime.now(timezone.utc)
                if user.pro_until and user.pro_until > now:
                    new_until = user.pro_until + timedelta(days=tariff["days"])
                else:
                    new_until = now + timedelta(days=tariff["days"])
                user.is_pro = True
                user.pro_until = new_until
            await send_pro_activated_message(message, new_until.strftime("%d.%m.%Y %H:%M"), auto_renew=False, is_test=False)
            return

    if payload.startswith("pro_"):
        tariff_id = payload.split("_")[1] + "_" + payload.split("_")[2]
        tariff = TARIFFS.get(tariff_id)
        if not tariff:
            await message.answer("❌ Неизвестный тариф.")
            return
        if total_amount != tariff["price_rub"]:
            await message.answer("❌ Некорректная сумма.")
            return
        if currency != "RUB":
            await message.answer("❌ Некорректная валюта.")
            return

        async with AsyncSessionLocal() as db:
            async with db.begin():
                user = await get_user(db, message.from_user.id)
                if not user:
                    user = await create_user(db, message.from_user.id, message.from_user.username)
                    city = await get_city_by_name(db, "Красноярск")
                    if city:
                        user.city_id = city.id
                        await db.flush()

                existing = await get_payment_by_telegram_charge_id(db, telegram_charge_id)
                if existing:
                    await message.answer("Этот платёж уже был обработан.")
                    return

                await create_payment(db, user.id, telegram_charge_id, provider_charge_id, total_amount, tariff="pro_month")
                now = datetime.now(timezone.utc)
                if user.pro_until and user.pro_until > now:
                    new_until = user.pro_until + timedelta(days=tariff["days"])
                else:
                    new_until = now + timedelta(days=tariff["days"])
                user.is_pro = True
                user.pro_until = new_until
            await send_pro_activated_message(message, new_until.strftime("%d.%m.%Y %H:%M"), auto_renew=False, is_test=False)
            return

    elif payload == "emergency_search_rub":
        async with AsyncSessionLocal() as db:
            user = await get_user(db, message.from_user.id)
            if not user:
                await message.answer("Сначала /start")
                return
            await grant_emergency_search(db, user.id)
        await message.answer(
            "✅ Оплата прошла успешно! Теперь вы можете использовать экстренный поиск.\n"
            "Нажмите «🚨 Бензин заканчивается!» и введите адрес.",
            reply_markup=main_menu_keyboard()
        )
        return

    await message.answer("⚠️ Неизвестный тип платежа. Обратитесь к администратору.")
