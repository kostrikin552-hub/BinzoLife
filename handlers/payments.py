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
STARS_PRICE = 150

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

    text = (
        "💎 BinzoLife PRO\n\n"
        "Всего 99 ₽ или 150 Stars в месяц — и вы получаете:\n\n"
        "✅ Безлимитный поиск АЗС (без ограничений)\n"
        "✅ Уведомления о снижении цены на выбранных АЗС\n"
        "✅ График изменения цены за 30 дней\n"
        "✅ Расчёт потенциальной экономии\n"
        "✅ Приоритетные уведомления (на 15 минут раньше)\n"
        "✅ Метка «PRO» в рейтинге репортёров\n\n"
        "🎁 Сейчас вы можете попробовать PRO бесплатно в течение 3 дней (активируется после первого поиска).\n\n"
        "Оплатить:\n"
        "• 150 Telegram Stars (мгновенно, без комиссии в Telegram)\n"
        "• 99 ₽ картой (через Telegram Payments)\n\n"
        "Выберите способ:"
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
    choice = callback.data.split("_")[3]
    auto_renew = (choice == "on")
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

@router.callback_query(F.data == "buy_pro_stars")
async def buy_pro_stars(callback: types.CallbackQuery):
    await callback.answer()
    prices = [LabeledPrice(label="PRO — 30 дней", amount=STARS_PRICE)]
    try:
        await callback.message.answer_invoice(
            title="💎 PRO — 30 дней",
            description=PRODUCT_DESCRIPTION,
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

    if payload.startswith("pro_month_30d"):
        if pre_checkout.total_amount != PRICE * 100:
            await pre_checkout.answer(ok=False, error_message="Некорректная сумма")
            return
        if pre_checkout.currency != "RUB":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
        async with AsyncSessionLocal() as db:
            user = await get_user(db, pre_checkout.from_user.id)
            if not user:
                await pre_checkout.answer(ok=False, error_message="Сначала выполните /start")
                return
        await pre_checkout.answer(ok=True)
    elif payload == "pro_month_stars":
        if pre_checkout.total_amount != STARS_PRICE:
            await pre_checkout.answer(ok=False, error_message="Некорректное количество Stars")
            return
        if pre_checkout.currency != "XTR":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
        await pre_checkout.answer(ok=True)
    elif payload == "emergency_search_rub":
        if pre_checkout.total_amount != 50 * 100:
            await pre_checkout.answer(ok=False, error_message="Некорректная сумма")
            return
        if pre_checkout.currency != "RUB":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
        await pre_checkout.answer(ok=True)
    elif payload == "emergency_search_stars":
        if pre_checkout.total_amount != 50:
            await pre_checkout.answer(ok=False, error_message="Некорректное количество Stars")
            return
        if pre_checkout.currency != "XTR":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
        await pre_checkout.answer(ok=True)
    elif payload == "test_payment_payload":
        if pre_checkout.from_user.id not in settings.admin_ids:
            await pre_checkout.answer(ok=False, error_message="Тестовый платёж доступен только администраторам")
            return
        if pre_checkout.currency != "RUB":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
        await pre_checkout.answer(ok=True)
    else:
        await pre_checkout.answer(ok=False, error_message="Неизвестный тип платежа")

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

# ===== ИСПРАВЛЕННАЯ ОБРАБОТКА УСПЕШНОГО ПЛАТЕЖА (Атомарная транзакция + уникальный индекс) =====
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
            async with db.begin():  # атомарная транзакция
                user = await get_user(db, message.from_user.id)
                if not user:
                    user = await create_user(db, message.from_user.id, message.from_user.username)
                    city = await get_city_by_name(db, "Красноярск")
                    if city:
                        user.city_id = city.id
                        await db.flush()
                # Проверка идемпотентности по charge_id
                existing = await get_payment_by_telegram_charge_id(db, telegram_charge_id)
                if existing:
                    await message.answer("Этот платёж уже был обработан.")
                    return
                await create_payment(db, user.id, telegram_charge_id, provider_charge_id, total_amount, currency=currency, tariff="test")
                await activate_pro(db, user, days=30)
                # Коммит автоматический при выходе из блока async with
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
        if total_amount != STARS_PRICE:
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
                    new_until = user.pro_until + timedelta(days=30)
                else:
                    new_until = now + timedelta(days=30)
                user.is_pro = True
                user.pro_until = new_until
                # коммит автоматический
        until = format_pro_until(user.pro_until)
        await send_pro_activated_message(message, until, auto_renew=False, is_test=False)
        return

    if payload.startswith("pro_month_30d"):
        auto_renew = False
        parts = payload.split("_")
        if len(parts) >= 5 and parts[4].isdigit():
            auto_renew = bool(int(parts[4]))
        else:
            logger.warning(f"Не удалось распарсить auto_renew: {payload}")

        if total_amount != PRICE:
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
                    new_until = user.pro_until + timedelta(days=30)
                else:
                    new_until = now + timedelta(days=30)
                user.is_pro = True
                user.pro_until = new_until
                user.auto_renew = auto_renew
                # коммит автоматический

        until = format_pro_until(user.pro_until)
        await send_pro_activated_message(message, until, auto_renew=auto_renew, is_test=False)
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
