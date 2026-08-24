from aiogram import Router, types, F
from aiogram.types import LabeledPrice, PreCheckoutQuery, SuccessfulPayment
from config import settings
from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_payment, activate_pro, get_payment_by_telegram_charge_id,
    is_user_pro, get_city_by_name
)
from services.subscription import format_pro_until
from keyboards.reply import main_menu_keyboard
from keyboards.inline import pro_purchase_keyboard
import logging
from datetime import datetime

from handlers.emergency import show_result
from utils.helpers import haversine_distance

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

async def send_invoice(message: types.Message, amount: int, payload: str, description: str):
    prices = [LabeledPrice(label=description, amount=amount * 100)]
    try:
        await message.answer_invoice(
            title=f"Оплата {amount} ₽",
            description=description,
            provider_token=settings.PROVIDER_TOKEN,
            currency="RUB",
            prices=prices,
            start_parameter="emergency_payment",
            payload=payload
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
            await message.answer(
                f"✅ Ваш PRO активен до {until}\n\n"
                "Спасибо, что поддерживаете проект!",
                reply_markup=main_menu_keyboard()
            )
            return

    text = (
        f"💎 <b>PRO — {PRICE} ₽ / 30 дней</b>\n\n"
        f"{PRODUCT_DESCRIPTION}\n\n"
        "Нажмите «Оплатить», чтобы активировать подписку."
    )
    await message.answer(text, reply_markup=pro_purchase_keyboard())

@router.callback_query(F.data == "buy_pro")
async def process_buy_pro(callback: types.CallbackQuery):
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
            payload="pro_month_30d"
        )
    except Exception as e:
        logger.error(f"Ошибка при создании инвойса: {e}")
        await callback.message.answer("❌ Не удалось создать платёж. Попробуйте позже.")

@router.pre_checkout_query()
async def pre_checkout_query(pre_checkout: PreCheckoutQuery):
    if pre_checkout.invoice_payload not in ["pro_month_30d", "emergency_search"]:
        await pre_checkout.answer(ok=False, error_message="Некорректный платёж.")
        return
    if pre_checkout.total_amount not in [PRICE * 100, 5000]:
        await pre_checkout.answer(ok=False, error_message="Некорректная сумма.")
        return
    if pre_checkout.currency != "RUB":
        await pre_checkout.answer(ok=False, error_message="Некорректная валюта.")
        return
    await pre_checkout.answer(ok=True)

@router.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payment: SuccessfulPayment = message.successful_payment
    telegram_charge_id = payment.telegram_payment_charge_id
    provider_charge_id = payment.provider_payment_charge_id
    total_amount = payment.total_amount / 100
    payload = payment.invoice_payload

    if payload == "emergency_search":
        # Разовый экстренный поиск
        # В реальном бою координаты нужно восстанавливать из кэша
        # Пока просто подтверждаем оплату
        await message.answer(
            "✅ Оплата прошла успешно!\n\n"
            "Теперь отправьте мне своё местоположение или напишите адрес, "
            "чтобы я нашёл ближайшую АЗС с топливом.\n\n"
            "Нажмите «🚨 Бензин заканчивается!» ещё раз.",
            reply_markup=main_menu_keyboard()
        )
        return

    if payload != "pro_month_30d":
        await message.answer("❌ Некорректный платёж.")
        return
    if total_amount != PRICE:
        await message.answer("❌ Некорректная сумма.")
        return
    if payment.currency != "RUB":
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

    until = format_pro_until(user.pro_until)
    await message.answer(
        f"✅ <b>PRO активирован до {until}</b>\n\n"
        "Теперь я буду следить за выгодными ценами и сообщать вам.\n"
        "Спасибо за поддержку!",
        reply_markup=main_menu_keyboard()
    )
    logger.info(f"User {user.telegram_id} активировал PRO до {user.pro_until}")
