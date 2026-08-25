# handlers/payments.py (полный, исправлен – удалён temp_auto_renew, логика упрощена)
import json
import logging
from aiogram import Router, types, F
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment, InlineKeyboardMarkup, InlineKeyboardButton
from config import settings
from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_user, create_payment, activate_pro, get_payment_by_telegram_charge_id,
    is_user_pro, get_city_by_name, update_user, grant_emergency_search
)
from services.subscription import format_pro_until
from keyboards.reply import main_menu_keyboard
from keyboards.inline import pro_purchase_keyboard
from datetime import datetime, timedelta, timezone

router = Router()
logger = logging.getLogger(__name__)

PRODUCT_TITLE = "💎 PRO — 30 дней"
PRODUCT_DESCRIPTION = (
    "🔔 Уведомления о снижении цены\n"
    "📉 Аномально низкая цена\n"
    "⛽ Появление топлива\n"
    "📊 Полная динамика цен\n"
    "💰 Расчёт потенциальной экономии\n"
    "📌 Следить за несколькими АЗС"
)
PRICE = 99
STARS_PRICE = 50

# Временное хранилище УДАЛЕНО – выбор сохраняется в payload

async def send_invoice(message: types.Message, amount: int, payload: str, description: str, currency: str = "RUB"):
    prices = [LabeledPrice(label=description, amount=amount * 100)]
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

    text = (
        f"💎 <b>PRO — {PRICE} ₽ / 30 дней</b>\n\n"
        f"{PRODUCT_DESCRIPTION}\n\n"
        "Нажмите «Оплатить», чтобы активировать подписку.\n"
        "После оплаты вы сможете включить автопродление."
    )
    await message.answer(text, reply_markup=pro_purchase_keyboard())

@router.callback_query(F.data == "buy_pro")
async def process_buy_pro(callback: types.CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Включить автопродление", callback_data="pro_auto_renew_on")],
        [InlineKeyboardButton(text="❌ Без автопродления", callback_data="pro_auto_renew_off")]
    ])
    await callback.message.edit_text(
        "💎 Перед оплатой выберите опцию:\n\n"
        "✅ Автопродление — мы напомним за 3 дня до окончания, и вы сможете продлить в один клик.\n"
        "❌ Без автопродления — подписка закончится через 30 дней, и вы получите уведомление.",
        reply_markup=kb
    )

@router.callback_query(lambda c: c.data.startswith("pro_auto_renew_"))
async def pro_auto_renew_choice(callback: types.CallbackQuery):
    choice = callback.data.split("_")[3]  # on или off
    auto_renew = (choice == "on")
    # Выбор сохраняется в payload, временное хранилище не используется
    await callback.answer()
    prices = [LabeledPrice(label="PRO — 30 дней", amount=PRICE * 100)]
    try:
        await callback.message.answer_invoice(
            title=PRODUCT_TITLE,
            description=PRODUCT_DESCRIPTION,
            provider_token=settings.PROVIDER_TOKEN,
            currency="RUB",
            prices=prices,
            start_parameter="pro_subscription",
            payload=f"pro_month_30d_auto_{int(auto_renew)}"
        )
    except Exception as e:
        logger.error(f"Ошибка при создании инвойса: {e}")
        await callback.message.answer("❌ Не удалось создать платёж. Попробуйте позже.")

@router.pre_checkout_query()
async def pre_checkout_query(pre_checkout: PreCheckoutQuery):
    payload = pre_checkout.invoice_payload
    if payload.startswith("pro_month_30d"):
        if pre_checkout.total_amount != PRICE * 100:
            await pre_checkout.answer(ok=False, error_message="Некорректная сумма.")
            return
        if pre_checkout.currency != "RUB":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта.")
            return
        await pre_checkout.answer(ok=True)
    elif payload == "emergency_search_rub":
        if pre_checkout.total_amount != 50 * 100:
            await pre_checkout.answer(ok=False, error_message="Некорректная сумма.")
            return
        if pre_checkout.currency != "RUB":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта.")
            return
        await pre_checkout.answer(ok=True)
    elif payload == "emergency_search_stars":
        if pre_checkout.total_amount != STARS_PRICE:
            await pre_checkout.answer(ok=False, error_message="Некорректное количество Stars.")
            return
        if pre_checkout.currency != "XTR":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта.")
            return
        await pre_checkout.answer(ok=True)
    else:
        await pre_checkout.answer(ok=False, error_message="Некорректный платёж.")

@router.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payment: SuccessfulPayment = message.successful_payment
    telegram_charge_id = payment.telegram_payment_charge_id
    provider_charge_id = payment.provider_payment_charge_id
    total_amount = payment.total_amount / 100
    payload = payment.invoice_payload
    currency = payment.currency

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

    if payload.startswith("pro_month_30d"):
        auto_renew = bool(int(payload.split("_")[3])) if len(payload.split("_")) > 3 else False
        if total_amount != PRICE:
            await message.answer("❌ Некорректная сумма.")
            return
        if currency != "RUB":
            await message.answer("❌ Некорректная валюта.")
            return

        async with AsyncSessionLocal() as db:
            user = await get_user(db, message.from_user.id)
            if not user:
                user = await create_user(db, message.from_user.id, message.from_user.username)
                city = await get_city_by_name(db, "Красноярск")
                if city:
                    user.city_id = city.id
                    await db.commit()

            existing = await get_payment_by_telegram_charge_id(db, telegram_charge_id)
            if existing:
                await message.answer("Этот платёж уже был обработан.")
                return

            async with db.begin():
                await create_payment(
                    db,
                    user.id,
                    telegram_charge_id,
                    provider_charge_id,
                    total_amount,
                    tariff="pro_month"
                )
                await activate_pro(db, user, days=30)
                user.auto_renew = auto_renew
                await db.commit()

        until = format_pro_until(user.pro_until)
        await message.answer(
            f"✅ <b>PRO активирован до {until}</b>\n"
            f"🔄 Автопродление: {'включено' if auto_renew else 'выключено'}\n\n"
            "Теперь вы будете получать уведомления о выгодных ценах и появлении топлива.\n"
            "Спасибо за поддержку!",
            reply_markup=main_menu_keyboard()
        )
        logger.info(f"User {user.telegram_id} активировал PRO до {user.pro_until}, auto_renew={auto_renew}")
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
