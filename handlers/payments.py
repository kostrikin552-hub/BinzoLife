# handlers/payments.py — ПОЛНАЯ ФИНАЛЬНАЯ ВЕРСИЯ
import json
import logging
from datetime import datetime, timedelta, timezone
from aiogram import Router, types, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    LabeledPrice, PreCheckoutQuery, SuccessfulPayment,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from config import settings
from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_user, create_payment, activate_pro,
    get_payment_by_telegram_charge_id, is_user_pro,
    get_city_by_name, grant_emergency_search,
    commit_or_rollback, get_recent_emergency_payment
)
from services.subscription import format_pro_until
from keyboards.reply import main_menu_keyboard

logger = logging.getLogger(__name__)
router = Router()

# ===== КОНСТАНТЫ ЭКСТРЕННОГО ПОИСКА (согласованы с emergency.py) =====
EMERGENCY_RUB_AMOUNT = 5000    # 50.00 ₽ (в копейках)
EMERGENCY_STARS_AMOUNT = 30    # 30 ⭐️


# ===== ОБНОВЛЁННАЯ ТАРИФНАЯ СЕТКА =====
# Разовый тариф на 24 часа — только Stars (микро-сумма, нельзя через эквайринг).
# Тариф выходного дня — ровно 50.00 ₽ (порог минимального чека эквайринга).
TARIFFS = {
    "pro_24h": {
        "title": "⚡ PRO на 24 часа",
        "short_desc": "Для разовой дальней поездки или теста",
        "full_desc": (
            "⚡ <b>PRO-доступ на 24 часа</b>\n\n"
            "🎯 <b>Кому идеально:</b> вам нужно заправиться прямо сейчас или предстоит разовая поездка по делам.\n\n"
            "🔹 Безлимитный поиск лучших цен с расчётом чистой выгоды\n"
            "🔹 Радар очередей и наличия топлива\n"
            "🔹 Мгновенная оплата в Telegram Stars (без ввода данных карты)\n\n"
            "💰 <b>Цена:</b> всего 15 ⭐️ Stars (~25–30 ₽)!"
        ),
        "rub_amount": 0,
        "stars_amount": 15,
        "days": 1,
        "only_stars": True,
        "badge": "⚡ Быстрый тест"
    },
    "pro_weekend": {
        "title": "🚗 PRO на Выходные (3 дня)",
        "short_desc": "Поездки на дачу и за город",
        "full_desc": (
            "🚗 <b>PRO на Выходные (3 дня)</b>\n\n"
            "🎯 <b>Кому идеально:</b> активные поездки в пятницу, субботу и воскресенье (дача, путешествия, загород).\n\n"
            "🔹 Полный доступ ко всем АЗС по трассе и в черте региона\n"
            "🔹 Графики изменения оптовых цен для выбора лучшего дня заправки\n"
            "🔹 SOS-режим «Сухой бак» включён бесплатно\n\n"
            "💰 <b>Цена:</b> 50.00 ₽ картой РФ / СБП или 30 ⭐️ Stars\n"
            "💡 <i>Окупается с первой же заправки бака на 30–40 литров!</i>"
        ),
        "rub_amount": 5000,
        "stars_amount": 30,
        "days": 3,
        "only_stars": False,
        "badge": "👍 Уикенд"
    },
    "pro_1m": {
        "title": "👑 PRO на 1 месяц (Хит продаж)",
        "short_desc": "Экономия до 2 000 ₽ в месяц",
        "full_desc": (
            "👑 <b>PRO на 1 месяц — Максимальный комфорт водителя</b>\n\n"
            "🎯 <b>Кому идеально:</b> регулярные поездки на работу, по городу и семейные дела.\n\n"
            "🔹 Безлимитные точные поиски лучших стел каждый день\n"
            "🔹 Автоматические уведомления при падении цен на любимых заправках\n"
            "🔹 Радар утренних пробок и свободных пистолетов\n"
            "🔹 Приоритетная поддержка и отсутствие рекламы\n\n"
            "💰 <b>Цена:</b> 99.00 ₽ картой РФ / СБП или 50 ⭐️ Stars\n"
            "💡 <i>Всего 3 рубля в день! Экономия с бака перекрывает стоимость подписки в 3–4 раза.</i>"
        ),
        "rub_amount": 9900,
        "stars_amount": 50,
        "days": 30,
        "only_stars": False,
        "badge": "🔥 Выбор 80% водителей"
    },
    "pro_3m": {
        "title": "💎 PRO на Сезон (3 месяца)",
        "short_desc": "Выгода 40% + защита от сезонных скачков",
        "full_desc": (
            "💎 <b>PRO на Сезон (3 месяца) — Топливный автопилот</b>\n\n"
            "🎯 <b>Кому идеально:</b> водителям, которые не хотят думать о подписках и ценят стабильную выгоду.\n\n"
            "🔹 Защита от сезонных скачков цен на бензин и ДТ\n"
            "🔹 Личный радар цен на 90 дней\n"
            "🔹 Включены все будущие обновления и новые АЗС региона\n\n"
            "💰 <b>Цена:</b> 249.00 ₽ картой РФ / СБП или 125 ⭐️ Stars\n"
            "💡 <i>Скидка 40% по сравнению с помесячной оплатой. Выгода до 6 000 ₽ за сезон!</i>"
        ),
        "rub_amount": 24900,
        "stars_amount": 125,
        "days": 90,
        "only_stars": False,
        "badge": "💰 Максимальная выгода"
    }
}


async def send_invoice(
    message: types.Message,
    amount: int,
    payload: str,
    description: str,
    currency: str = "RUB",
    need_email: bool = False
):
    """
    Отправляет инвойс с корректным чеком 54-ФЗ для российских эквайрингов.
    """
    prices = [LabeledPrice(label=description[:30], amount=amount)]
    provider_token = settings.PAYMENT_PROVIDER_TOKEN if currency == "RUB" else ""
    provider_data = None

    if currency == "RUB":
        rub_str = f"{amount / 100:.2f}"
        receipt_data = {
            "receipt": {
                "items": [{
                    "description": description[:64],
                    "quantity": "1.00",
                    "amount": {"value": rub_str, "currency": "RUB"},
                    "vat_code": 1,
                    "payment_mode": "full_payment",
                    "payment_subject": "service"
                }]
            }
        }
        provider_data = json.dumps(receipt_data, ensure_ascii=False)

    try:
        await message.answer_invoice(
            title=f"Оплата: {description[:24]}",
            description=f"BinzoLife PRO: {description}",
            provider_token=provider_token,
            currency=currency,
            prices=prices,
            start_parameter="binzolife-pro",
            payload=payload,
            need_email=need_email,
            send_email_to_provider=need_email,
            provider_data=provider_data
        )
        logger.info(f"Инвойс отправлен: {description}, {amount/100:.2f} {currency}")
    except Exception as e:
        logger.error(f"Ошибка отправки инвойса ({currency}, {amount}): {e}")
        if currency == "RUB" and provider_data:
            try:
                await message.answer_invoice(
                    title=f"Оплата: {description[:24]}",
                    description=f"BinzoLife PRO: {description}",
                    provider_token=provider_token,
                    currency=currency,
                    prices=prices,
                    start_parameter="binzolife-pro",
                    payload=payload
                )
                return
            except Exception as e2:
                logger.error(f"Повторная ошибка отправки инвойса: {e2}")
        await message.answer(
            "❌ Не удалось сформировать счёт на оплату картой.\n"
            "Пожалуйста, воспользуйтесь быстрой оплатой через ⭐️ <b>Telegram Stars</b>.",
            parse_mode="HTML"
        )


# ====================== ОСНОВНОЙ ХЕНДЛЕР "💎 PRO" ======================
@router.message(F.text == "💎 PRO")
async def show_pro_info(message: types.Message, user_telegram_id: int = None):
    """
    Показ PRO-тарифов. Если вызвано из callback — user_telegram_id обязателен,
    т.к. message.from_user.id в этом случае = ID бота.
    """
    target_user_id = user_telegram_id if user_telegram_id else message.from_user.id
    logger.info(f"[show_pro_info] target={target_user_id}, from_message={message.from_user.id}, from_callback={user_telegram_id}")

    async with AsyncSessionLocal() as db:
        user = await get_user(db, target_user_id)
        if not user:
            await message.answer("Сначала выполните /start")
            return
        pro_active = await is_user_pro(db, user)
        if pro_active and user.pro_until:
            until = format_pro_until(user.pro_until)
            auto_renew = "включено" if user.auto_renew else "выключено"
            await message.answer(
                f"✅ <b>Ваш PRO-статус активен!</b>\n\n"
                f"📅 Действует до: <b>{until}</b>\n"
                f"🔄 Автопродление: {auto_renew}\n\n"
                f"Вам открыт радар цен, уведомления о скидках и безлимитный поиск без очередей.",
                reply_markup=main_menu_keyboard(),
                parse_mode="HTML"
            )
            return

    sales_text = (
        "💎 <b>BinzoLife PRO — ваш персональный топливный штурман</b>\n\n"
        "Перестаньте переплачивать за бензин на случайных заправках:\n\n"
        "🚀 <b>Что даёт PRO:</b>\n"
        "• <b>Расчёт чистой выгоды:</b> бот учитывает расход авто на дорогу до заправки\n"
        "• <b>Радар очередей и наличия:</b> вы точно знаете, есть ли топливо на колонке\n"
        "• <b>Графики оптовых цен:</b> заправляйтесь в дни спада стоимости\n"
        "• <b>SOS «Сухой бак»:</b> гарантия докатки до ближайшей АЗС без доплат\n"
        "• <b>Уведомления о скидках:</b> моментальный сигнал, когда цена на стеле падает\n\n"
        "💡 <i>В среднем PRO экономит водителю от 450 до 2 200 ₽ в месяц и окупается с первой заправки!</i>\n\n"
        "👇 <b>Выберите подходящий тариф:</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ 24 часа — 15 ⭐️ Stars (Быстрый тест)", callback_data="buy_tariff_pro_24h")],
        [InlineKeyboardButton(text="🚗 На Выходные (3 дня) — 50 ₽ / 30 ⭐️", callback_data="buy_tariff_pro_weekend")],
        [InlineKeyboardButton(text="👑 1 месяц — 99 ₽ / 50 ⭐️ (Хит)", callback_data="buy_tariff_pro_1m")],
        [InlineKeyboardButton(text="💎 3 месяца — 249 ₽ / 125 ⭐️ (Сезон)", callback_data="buy_tariff_pro_3m")],
    ])
    await message.answer(sales_text, reply_markup=kb, parse_mode="HTML")


# ====================== ВЫБОР СПОСОБА ОПЛАТЫ ======================
@router.callback_query(lambda c: c.data.startswith("buy_tariff_"))
async def choose_payment_method(callback: types.CallbackQuery):
    tariff_key = callback.data.replace("buy_tariff_", "")
    if tariff_key not in TARIFFS:
        await callback.answer("Тариф не найден", show_alert=True)
        return
    t = TARIFFS[tariff_key]
    rub_price = t["rub_amount"] // 100

    keyboard_rows = []
    if not t.get("only_stars") and settings.PAYMENT_PROVIDER_TOKEN and rub_price >= 50:
        keyboard_rows.append([
            InlineKeyboardButton(
                text=f"💳 Картой РФ / СБП ({rub_price} ₽)",
                callback_data=f"pay_rub:{tariff_key}"
            )
        ])
    keyboard_rows.append([
        InlineKeyboardButton(
            text=f"⭐️ Telegram Stars ({t['stars_amount']} ⭐️)",
            callback_data=f"pay_stars:{tariff_key}"
        )
    ])
    keyboard_rows.append([
        InlineKeyboardButton(text="◀️ Назад к тарифам", callback_data="back_to_tariffs")
    ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

    payment_hint = ""
    if t.get("only_stars"):
        payment_hint = "\n💡 <i>Этот микротариф оплачивается только в Stars (мгновенно, без ввода данных карты).</i>\n"

    try:
        await callback.message.edit_text(
            f"{t['full_desc']}\n"
            f"{payment_hint}\n"
            f"<b>Выберите способ оплаты:</b>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    except Exception:
        await callback.message.answer(
            f"{t['full_desc']}\n{payment_hint}\n<b>Выберите способ оплаты:</b>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    await callback.answer()


@router.callback_query(F.data == "back_to_tariffs")
async def back_to_tariffs(callback: types.CallbackQuery):
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        pass
    # ВАЖНО: передаём реальный user_telegram_id из callback
    await show_pro_info(callback.message, user_telegram_id=callback.from_user.id)


@router.callback_query(F.data == "buy_pro")
async def buy_pro_legacy(callback: types.CallbackQuery):
    await callback.answer()
    # ВАЖНО: передаём реальный user_telegram_id из callback
    await show_pro_info(callback.message, user_telegram_id=callback.from_user.id)


# ====================== ОПЛАТА РУБЛЯМИ ======================
@router.callback_query(F.data.startswith("pay_rub:"))
async def send_rub_invoice(callback: types.CallbackQuery):
    tariff_key = callback.data.split(":")[1]
    t = TARIFFS.get(tariff_key)
    if not t or t.get("only_stars"):
        await callback.answer("Оплата картой недоступна для этого тарифа", show_alert=True)
        return
    if not settings.PAYMENT_PROVIDER_TOKEN:
        await callback.answer("💳 Оплата картой временно недоступна. Используйте Stars.", show_alert=True)
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


# ====================== ОПЛАТА STARS ======================
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


# ====================== PRE-CHECKOUT ======================
@router.pre_checkout_query()
async def pre_checkout_query(pre_checkout: PreCheckoutQuery):
    logger.info(f"Pre-checkout: payload={pre_checkout.invoice_payload}, total={pre_checkout.total_amount}, currency={pre_checkout.currency}")
    payload = pre_checkout.invoice_payload
    if not payload:
        await pre_checkout.answer(ok=False, error_message="Некорректный платёж")
        return

    # ===== Экстренный поиск (RUB) =====
    if payload.startswith("emergency_rub_"):
        parts = payload.split("_")
        if len(parts) >= 3 and int(parts[2]) != pre_checkout.from_user.id:
            await pre_checkout.answer(ok=False, error_message="Неверный заказ")
            return
        if pre_checkout.total_amount != EMERGENCY_RUB_AMOUNT:
            await pre_checkout.answer(ok=False, error_message="Некорректная сумма (требуется 50 ₽)")
            return
        if pre_checkout.currency != "RUB":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
        await pre_checkout.answer(ok=True)
        return

    # ===== Экстренный поиск (Stars) =====
    if payload.startswith("emergency_stars_"):
        parts = payload.split("_")
        if len(parts) >= 3 and int(parts[2]) != pre_checkout.from_user.id:
            await pre_checkout.answer(ok=False, error_message="Неверный заказ")
            return
        if pre_checkout.total_amount != EMERGENCY_STARS_AMOUNT:
            await pre_checkout.answer(ok=False, error_message="Некорректное количество Stars")
            return
        if pre_checkout.currency != "XTR":
            await pre_checkout.answer(ok=False, error_message="Некорректная валюта")
            return
        await pre_checkout.answer(ok=True)
        return

    # ===== PRO-подписки =====
    parts = payload.split("_")
    if len(parts) < 3:
        await pre_checkout.answer(ok=False, error_message="Некорректный идентификатор заказа")
        return

    pay_type = parts[0]
    tariff_key = f"{parts[1]}_{parts[2]}"
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        await pre_checkout.answer(ok=False, error_message="Тариф не найден")
        return

    if pay_type == "rub":
        if tariff.get("only_stars"):
            await pre_checkout.answer(ok=False, error_message="Этот тариф доступен только в Telegram Stars")
            return
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


# ====================== УСПЕШНАЯ ОПЛАТА ======================
@router.message(F.successful_payment)
async def successful_payment(message: types.Message):
    payment: SuccessfulPayment = message.successful_payment
    telegram_charge_id = payment.telegram_payment_charge_id
    provider_charge_id = payment.provider_payment_charge_id or "STARS"
    total_amount = payment.total_amount / 100 if payment.currency == "RUB" else payment.total_amount
    payload = payment.invoice_payload
    currency = payment.currency

    logger.info(f"Получен успешный платёж: payload={payload}, currency={currency}, amount={total_amount}")

    # ===== ЭКСТРЕННЫЙ ПОИСК =====
    if payload.startswith("emergency_rub_") or payload.startswith("emergency_stars_"):
        async with AsyncSessionLocal() as db:
            existing = await get_payment_by_telegram_charge_id(db, telegram_charge_id)
            if existing:
                await message.answer("✅ Этот платёж уже был обработан.")
                return

            user = await get_user(db, message.from_user.id)
            if not user:
                user = await create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)

            await create_payment(
                db, user.id, telegram_charge_id, provider_charge_id,
                total_amount, currency=currency, tariff="emergency"
            )
            await grant_emergency_search(db, user.id)
            user.emergency_access_until = datetime.now(timezone.utc) + timedelta(hours=2)
            await commit_or_rollback(db)

        await message.answer(
            "✅ <b>Оплата прошла успешно!</b>\n\n"
            "🚨 Экстренный поиск активирован на <b>2 часа</b>.\n"
            "Нажмите «🚨 Бензин заканчивается!» и отправьте местоположение — я найду ближайшую работающую колонку.",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML"
        )
        return

    # ===== PRO-ПОДПИСКА =====
    parts = payload.split("_")
    if len(parts) < 3:
        await message.answer("⚠️ Неизвестный тип платежа. Обратитесь к администратору.")
        return

    tariff_key = f"{parts[1]}_{parts[2]}"
    tariff = TARIFFS.get(tariff_key)
    if not tariff:
        await message.answer("⚠️ Тариф не найден. Обратитесь к администратору.")
        return
    days_to_add = tariff["days"]

    async with AsyncSessionLocal() as db:
        existing = await get_payment_by_telegram_charge_id(db, telegram_charge_id)
        if existing:
            await message.answer("✅ Этот платёж уже был обработан.")
            return

        user = await get_user(db, message.from_user.id)
        if not user:
            user = await create_user(db, message.from_user.id, message.from_user.username, message.from_user.first_name)

        await create_payment(
            db, user.id, telegram_charge_id, provider_charge_id,
            total_amount, currency=currency, tariff=tariff_key
        )
        await activate_pro(db, user, days=days_to_add)
        await commit_or_rollback(db)

    until = format_pro_until(user.pro_until)
    await message.answer(
        f"🎉 <b>Оплата прошла успешно! Поздравляем!</b>\n\n"
        f"👑 <b>PRO-статус активирован на {days_to_add} дн.</b>\n"
        f"📅 Действует до: <b>{until}</b>\n\n"
        f"Теперь вам доступны: безлимитный поиск, честный расчёт чистой выгоды с учётом расхода на дорогу и радар скидок!",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard()
    )


# ====================== БЫСТРЫЕ ПЕРЕХОДЫ ======================
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
    # Передаём реальный user_telegram_id из callback
    await show_profile(callback.message, user_telegram_id=callback.from_user.id)
