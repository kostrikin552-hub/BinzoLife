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
        "rub_amount": 2900,   # 29.00 ₽
        "stars_amount": 15,
        "days": 1,
        "badge": "Для быстрой заправки"
    },
    "pro_1m": {
        "title": "👑 PRO 1 месяц (Хит)",
        "rub_amount": 9900,   # 99.00 ₽
        "stars_amount": 50,
        "days": 30,
        "badge": "Выгода 70%"
    },
    "pro_3m": {
        "title": "🔥 PRO 3 месяца (Сезон)",
        "rub_amount": 24900,  # 249.00 ₽
        "stars_amount": 125,
        "days": 90,
        "badge": "Максимальная экономия"
    }
}
# =================================

async def send_invoice(message: types.Message, amount: int, payload: str, description: str, currency: str = "RUB", need_email: bool = False):
    prices = [LabeledPrice(label=description, amount=amount)]
    try:
        await message.answer_invoice(
            title=f"Оплата {description}",
            description=description,
            provider_token=settings.PROVIDER_TOKEN if currency == "RUB" else "",
            currency=currency,
            prices=prices,
            start_parameter="payment",
            payload=payload,
            need_email=need_email,
            send_email_to_provider=need_email,
            provider_data=json.dumps({"receipt": {"items": [{"description": description, "quantity": "1", "amount": {"value": str(amount / 100 if currency == "RUB" else amount), "currency": currency}}]}}) if currency == "RUB" else None
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

    text = """
👑 <b>ПОЛУЧИТЕ ПОЛНЫЙ КОНТРОЛЬ НАД РАСХОДАМИ НА ТОПЛИВО</b>

Средний водитель переплачивает <b>от 4 800 до 11 000 ₽ в год</b>, заправляясь на привычных АЗС всего на 1.5–3 ₽ дороже.

⚡ <b>Что даёт PRO-подписка:</b>
• 🎯 <b>Радар низких цен:</b> мгновенный поиск скрытых скидок и акций вокруг вас.
• 🔔 <b>Уведомления об удешевлении:</b> бот маякнет, когда цена на вашей АЗС упадёт.
• 💰 <b>Калькулятор чистой выгоды:</b> расчет расхода с учётом расстояния до заправки.
• 🚫 <b>Безлимитный поиск</b> без задержек и ограничений.

👇 <b>Выберите удобный тариф (окупается с первой заправки):</b>
"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ 29 ₽ / 24ч", callback_data="buy_tariff_pro_24h"),
         InlineKeyboardButton(text="👑 99 ₽ / мес", callback_data="buy_tariff_pro_1m")],
        [InlineKeyboardButton(text="🔥 249 ₽ / 3 мес", callback_data="buy_tariff_pro_3m")],
    ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@router.callback_query(lambda c: c.data.startswith("buy_tariff_"))
async def choose_payment_method(callback: types.CallbackQuery):
    tariff_key = callback.data.replace("buy_tariff_", "")
    if tariff_key not in TARIFFS:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    t = TARIFFS[tariff_key]
    rub_price = t["rub_amount"] // 100

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Картой РФ / СБП ({rub_price} ₽)", callback_data=f"pay_rub:{tariff_key}")],
        [InlineKeyboardButton(text=f"⭐ Telegram Stars ({t['stars_amount']} ⭐)", callback_data=f"pay_stars:{tariff_key}")],
        [InlineKeyboardButton(text="◀️ Назад к тарифам", callback_data="back_to_tariffs")]
    ])
    await callback.message.edit_text(
        f"<b>{t['title']}</b>\n\n"
        f"{t['badge']}\n"
        f"Длительность: {t['days']} дней\n\n"
        f"Выберите удобный способ оплаты:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data == "back_to_tariffs")
async def back_to_tariffs(callback: types.CallbackQuery):
    await callback.answer()
    await show_pro_info(callback.message)

@router.callback_query(F.data.startswith("pay_rub:"))
async def send_rub_invoice(callback: types.CallbackQuery):
    tariff_key = callback.data.split(":")[1]
    t = TARIFFS.get(tariff_key)
    if not t:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    if not settings.PROVIDER_TOKEN:
        await callback.answer("Оплата картой временно недоступна. Воспользуйтесь Stars.", show_alert=True)
        return
    payload = f"rub_{tariff_key}_{callback.from_user.id}_{int(datetime.now().timestamp())}"
    await send_invoice(
        message=callback.message,
        amount=t["rub_amount"],
        payload=payload,
        description=t["title"],
        currency="RUB",
        need_email=True
    )
    await callback.answer()

@router.callback_query(F.data.startswith("pay_stars:"))
async def send_stars_invoice(callback: types.CallbackQuery):
    tariff_key = callback.data.split(":")[1]
    t = TARIFFS.get(tariff_key)
    if not t:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    payload = f"stars_{tariff_key}_{callback.from_user.id}_{int(datetime.now().timestamp())}"
    await send_invoice(
        message=callback.message,
        amount=t["stars_amount"],
        payload=payload,
        description=t["title"],
        currency="XTR",
        need_email=False
    )
    await callback.answer()

@router.pre_checkout_query()
async def pre_checkout_query(pre_checkout: PreCheckoutQuery):
    payload = pre_checkout.invoice_payload
    if not payload:
        await pre_checkout.answer(ok=False, error_message="Некорректный платёж")
        return

    # ===== ОБРАБОТКА EMERGENCY =====
    if payload.startswith("emergency_rub_"):
        parts = payload.split("_")
        if len(parts) >= 3 and int(parts[2]) != pre_checkout.from_user.id:
            await pre_checkout.answer(ok=False, error_message="Неверный заказ")
            return
        if pre_checkout.total_amount != 5000:
            await pre_checkout.answer(ok=False, error_message="Некорректная сумма")
            return
        if pre_checkout.currency != "RUB":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
        await pre_checkout.answer(ok=True)
        return

    if payload.startswith("emergency_stars_"):
        parts = payload.split("_")
        if len(parts) >= 3 and int(parts[2]) != pre_checkout.from_user.id:
            await pre_checkout.answer(ok=False, error_message="Неверный заказ")
            return
        if pre_checkout.total_amount != 50:
            await pre_checkout.answer(ok=False, error_message="Некорректное количество Stars")
            return
        if pre_checkout.currency != "XTR":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
        await pre_checkout.answer(ok=True)
        return

    # ===== ОБРАБОТКА PRO =====
    parts = payload.split("_")
    if len(parts) < 3:
        await pre_checkout.answer(ok=False, error_message="Некорректный идентификатор заказа")
        return

    pay_type = parts[0]  # rub или stars
    tariff_key = f"{parts[1]}_{parts[2]}"  # pro_24h, pro_1m, pro_3m
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        await pre_checkout.answer(ok=False, error_message="Тариф не найден")
        return

    if pay_type == "rub":
        if pre_checkout.total_amount != tariff["rub_amount"]:
            await pre_checkout.answer(ok=False, error_message="Некорректная сумма")
            return
        if pre_checkout.currency != "RUB":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
    elif pay_type == "stars":
        if pre_checkout.total_amount != tariff["stars_amount"]:
            await pre_checkout.answer(ok=False, error_message="Некорректное количество Stars")
            return
        if pre_checkout.currency != "XTR":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
    else:
        await pre_checkout.answer(ok=False, error_message="Неизвестный способ оплаты")
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, pre_checkout.from_user.id)
        if not user:
            await pre_checkout.answer(ok=False, error_message="Сначала выполните /start")
            return

    await pre_checkout.answer(ok=True)

# ========== ИСПРАВЛЕННЫЙ ОБРАБОТЧИК УСПЕШНОЙ ОПЛАТЫ (идемпотентность) ==========
@router.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payment: SuccessfulPayment = message.successful_payment
    telegram_charge_id = payment.telegram_payment_charge_id
    provider_charge_id = payment.provider_payment_charge_id or "STARS"
    total_amount = payment.total_amount / 100 if payment.currency == "RUB" else payment.total_amount
    payload = payment.invoice_payload
    currency = payment.currency

    logger.info(f"Получен успешный платёж: payload={payload}, currency={currency}, amount={total_amount}")

    # ===== ОБРАБОТКА EMERGENCY =====
    if payload.startswith("emergency_rub_") or payload.startswith("emergency_stars_"):
        async with AsyncSessionLocal() as db:
            existing = await get_payment_by_telegram_charge_id(db, telegram_charge_id)
            if existing:
                await message.answer("✅ Этот платёж уже был обработан.")
                return

            user = await get_user(db, message.from_user.id)
            if not user:
                user = await create_user(db, message.from_user.id, message.from_user.username)
                city = await get_city_by_name(db, "Красноярск")
                if city:
                    user.city_id = city.id
                    await db.commit()

            await create_payment(db, user.id, telegram_charge_id, provider_charge_id, total_amount, currency=currency, tariff="emergency")
            await grant_emergency_search(db, user.id)
            await db.commit()

        await message.answer(
            "✅ Оплата прошла успешно! Теперь вы можете использовать экстренный поиск.\n"
            "Нажмите «🚨 Бензин заканчивается!» и отправьте местоположение.",
            reply_markup=main_menu_keyboard()
        )
        return

    # ===== ОБРАБОТКА PRO =====
    parts = payload.split("_")
    if len(parts) < 3:
        await message.answer("⚠️ Неизвестный тип платежа. Обратитесь к администратору.")
        return

    pay_type = parts[0]
    tariff_key = f"{parts[1]}_{parts[2]}"
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        await message.answer("⚠️ Тариф не найден. Обратитесь к администратору.")
        return
    days_to_add = tariff["days"]

    async with AsyncSessionLocal() as db:
        # Проверка идемпотентности
        existing = await get_payment_by_telegram_charge_id(db, telegram_charge_id)
        if existing:
            await message.answer("✅ Этот платёж уже был обработан.")
            return

        user = await get_user(db, message.from_user.id)
        if not user:
            user = await create_user(db, message.from_user.id, message.from_user.username)
            city = await get_city_by_name(db, "Красноярск")
            if city:
                user.city_id = city.id
                await db.commit()

        await create_payment(db, user.id, telegram_charge_id, provider_charge_id, total_amount, currency=currency, tariff=tariff_key)
        await activate_pro(db, user, days=days_to_add)
        await db.commit()

    until = format_pro_until(user.pro_until)
    await message.answer(
        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
        f"👑 <b>PRO-статус активирован на {days_to_add} дн.</b>\n"
        f"📅 Действует до: <b>{until}</b>\n\n"
        f"Вам открыт радар цен, уведомления о скидках и безлимитный поиск!",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )

# ===== ОСТАЛЬНЫЕ ОБРАБОТЧИКИ (без изменений) =====
@router.callback_query(F.data == "buy_pro")
async def buy_pro_legacy(callback: types.CallbackQuery):
    await callback.answer()
    await show_pro_info(callback.message)

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
