# handlers/emergency.py — ПОЛНАЯ ФИНАЛЬНАЯ ВЕРСИЯ
import logging
import html
from datetime import datetime, timezone, timedelta
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, get_city_by_id, find_nearest_green_station,
    get_latest_fresh_price, save_user_location,
    has_emergency_access, get_recent_emergency_payment,
    commit_or_rollback
)
from database.models import FuelType, User
from utils.helpers import haversine_distance
from utils.geocoder import geocode_address
from services.subscription import is_user_pro_by_id
from keyboards.reply import main_menu_keyboard
from config import settings
from handlers.payments import (
    send_invoice,
    EMERGENCY_RUB_AMOUNT,
    EMERGENCY_STARS_AMOUNT
)

logger = logging.getLogger(__name__)
router = Router()


class EmergencyStates(StatesGroup):
    waiting_address = State()
    waiting_payment = State()


EMERGENCY_INTRO_TEXT = (
    "🚨 <b>Экстренный режим «Сухой бак»</b>\n\n"
    "Остались считанные литры? Мы поможем дотянуть до заправки:\n"
    "• Найдём <b>ближайшую работающую АЗС</b> в радиусе докатки (до 5–10 км)\n"
    "• Проверим <b>гарантированное наличие</b> вашего топлива на колонке\n"
    "• Проложим мгновенный прямой маршрут в Яндекс.Навигатор в 1 тап\n\n"
    "📍 <i>Отправьте вашу геолокацию кнопкой ниже:</i>"
)


def emergency_payment_keyboard() -> InlineKeyboardMarkup:
    kb_rows = []
    if settings.PAYMENT_PROVIDER_TOKEN:
        kb_rows.append([
            InlineKeyboardButton(text="💳 Картой РФ / СБП (50 ₽)", callback_data="pay_emergency_rub")
        ])
    kb_rows.append([
        InlineKeyboardButton(text="⭐️ Telegram Stars (30 ⭐️)", callback_data="pay_emergency_stars")
    ])
    kb_rows.append([
        InlineKeyboardButton(text="👑 Включить полный PRO (от 50 ₽)", callback_data="buy_pro")
    ])
    kb_rows.append([
        InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_menu")
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb_rows)


# ====================== СТАРТ ЭКСТРЕННОГО ПОИСКА ======================
@router.message(F.text == "🚨 Бензин заканчивается!")
async def emergency_start(message: types.Message, state: FSMContext):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user or not user.city_id:
            await message.answer(
                "⚠️ Сначала выберите город в профиле.\n"
                "Нажмите /start или настройте город в разделе «Профиль».",
                reply_markup=main_menu_keyboard()
            )
            return

        if user.last_lat and user.last_lon:
            kb = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="📍 Использовать мою геопозицию")],
                    [KeyboardButton(text="📎 Отправить новую геолокацию", request_location=True)],
                    [KeyboardButton(text="✏️ Ввести адрес вручную")],
                    [KeyboardButton(text="❌ Отмена")],
                ],
                resize_keyboard=True
            )
            await message.answer(
                "🚨 У меня есть твоя сохранённая геопозиция!\n"
                "Нажми «Использовать мою геопозицию», чтобы найти ближайшую АЗС, или отправь новую.",
                reply_markup=kb
            )
            return

    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить местоположение", request_location=True)],
            [KeyboardButton(text="✏️ Ввести адрес вручную")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True
    )
    await state.set_state(EmergencyStates.waiting_address)
    await message.answer(EMERGENCY_INTRO_TEXT, reply_markup=kb, parse_mode="HTML")


@router.message(F.text == "📍 Использовать мою геопозицию")
async def use_saved_location(message: types.Message, state: FSMContext):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user or not user.last_lat or not user.last_lon:
            await message.answer("❌ У меня нет твоей геопозиции. Отправь её, пожалуйста.")
            return
        lat = user.last_lat
        lon = user.last_lon
        await state.update_data(lat=lat, lon=lon)
        await check_availability_and_offer(message, state, lat, lon)


@router.message(EmergencyStates.waiting_address, F.location)
async def emergency_location(message: types.Message, state: FSMContext):
    lat = message.location.latitude
    lon = message.location.longitude
    await state.update_data(lat=lat, lon=lon)
    async with AsyncSessionLocal() as db:
        await save_user_location(db, message.from_user.id, lat, lon)
    await check_availability_and_offer(message, state, lat, lon)


@router.message(EmergencyStates.waiting_address, F.text)
async def emergency_address(message: types.Message, state: FSMContext):
    address = message.text.strip()
    if not address:
        await message.answer("❌ Адрес не может быть пустым. Попробуйте снова.")
        return

    if address.lower() in ("❌ отмена", "❌ отмена."):
        await state.clear()
        await message.answer("❌ Поиск отменён.", reply_markup=main_menu_keyboard())
        return

    coords = await geocode_address(address)
    if not coords:
        await message.answer(
            "❌ Не удалось определить координаты по этому адресу.\n"
            "Пожалуйста, уточните адрес или отправьте геолокацию.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="📍 Отправить местоположение", request_location=True)]],
                resize_keyboard=True
            )
        )
        return

    lat, lon = coords
    await state.update_data(lat=lat, lon=lon)
    async with AsyncSessionLocal() as db:
        await save_user_location(db, message.from_user.id, lat, lon)
    await check_availability_and_offer(message, state, lat, lon)


# ====================== ПРОВЕРКА ДОСТУПНОСТИ И ПРЕДЛОЖЕНИЕ ОПЛАТЫ ======================
async def check_availability_and_offer(message: types.Message, state: FSMContext, lat: float, lon: float):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        city = await get_city_by_id(db, user.city_id)
        if not city:
            await message.answer("❌ Город не найден. Сначала выберите город.", reply_markup=main_menu_keyboard())
            await state.clear()
            return

        has_access = await has_emergency_access(user, db)

        # Определяем топливо пользователя
        user_fuel = "АИ-95"
        if user.default_fuel:
            user_fuel = getattr(user.default_fuel, "value", "АИ-95")

        fuel_type = FuelType.AI_95
        for ft in FuelType:
            if ft.value == user_fuel or ft.name == str(user_fuel):
                fuel_type = ft
                break

        # 1. Поиск в 5 км
        station = await find_nearest_green_station(
            db, city.id, lat, lon, radius_km=5.0, fuel_type=fuel_type
        )

        # 2. Если есть доступ – расширяем радиус до 15 км
        if not station and has_access:
            station = await find_nearest_green_station(
                db, city.id, lat, lon, radius_km=15.0, fuel_type=fuel_type
            )
            if not station:
                await message.answer(
                    f"❌ В радиусе 15 км не найдено работающих АЗС с топливом {fuel_type.value}.\n"
                    "Попробуйте обычный поиск в главном меню.",
                    reply_markup=main_menu_keyboard()
                )
                await state.clear()
                return
            await show_result(message, station, lat, lon, fuel_type)
            await state.clear()
            return

        # 3. Если доступа нет – ищем ориентировочную станцию для показа дистанции
        if not station:
            preview_station = await find_nearest_green_station(
                db, city.id, lat, lon, radius_km=25.0, fuel_type=fuel_type
            )
            if not preview_station:
                await message.answer(
                    "❌ Поблизости не найдено работающих АЗС. Попробуйте обычный поиск в меню.",
                    reply_markup=main_menu_keyboard()
                )
                await state.clear()
                return

            dist = haversine_distance(lat, lon, preview_station.latitude, preview_station.longitude)
            await state.set_state(EmergencyStates.waiting_payment)
            await message.answer(
                f"🔍 <b>Срочный радар:</b> колонка с топливом {fuel_type.value} найдена!\n\n"
                f"📍 Ближайшая доступная АЗС — примерно в <b>{dist:.1f} км</b> от вас.\n\n"
                f"Для открытия точного адреса, цены и мгновенного маршрута в Яндекс.Навигатор "
                f"активируйте экстренный доступ (действует 2 часа):",
                reply_markup=emergency_payment_keyboard(),
                parse_mode="HTML"
            )
            return

        # 4. Станция найдена в 5 км
        dist = haversine_distance(lat, lon, station.latitude, station.longitude)

        if has_access:
            await show_result(message, station, lat, lon, fuel_type)
            await state.clear()
        else:
            await state.set_state(EmergencyStates.waiting_payment)
            await message.answer(
                f"🔍 <b>Срочный радар:</b> колонка с топливом {fuel_type.value} найдена!\n\n"
                f"📍 Ближайшая работающая АЗС — всего в <b>{dist:.1f} км</b> от вас.\n\n"
                f"Чтобы увидеть бренд, адрес, цену и проложить прямой маршрут в 1 клик, "
                f"активируйте экстренный доступ на 2 часа:",
                reply_markup=emergency_payment_keyboard(),
                parse_mode="HTML"
            )


# ====================== ОПЛАТА РУБЛЯМИ ======================
@router.callback_query(F.data == "pay_emergency_rub")
async def pay_emergency_rub(callback: types.CallbackQuery):
    await callback.answer()
    if not settings.PAYMENT_PROVIDER_TOKEN:
        await callback.message.answer("❌ Оплата картой временно недоступна. Воспользуйтесь Telegram Stars.")
        return

    user_id = callback.from_user.id
    order_id = f"emergency_rub_{user_id}_{int(datetime.now().timestamp())}"

    await send_invoice(
        message=callback.message,
        amount=EMERGENCY_RUB_AMOUNT,
        payload=order_id,
        description="Экстренный поиск АЗС (2 часа)",
        currency="RUB",
        need_email=True
    )


# ====================== ОПЛАТА STARS ======================
@router.callback_query(F.data == "pay_emergency_stars")
async def pay_emergency_stars(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    order_id = f"emergency_stars_{user_id}_{int(datetime.now().timestamp())}"

    await send_invoice(
        message=callback.message,
        amount=EMERGENCY_STARS_AMOUNT,
        payload=order_id,
        description="Экстренный поиск АЗС (2 часа)",
        currency="XTR",
        need_email=False
    )


# ====================== ПОКАЗ РЕЗУЛЬТАТА ======================
async def show_result(message: types.Message, station, lat: float, lon: float, fuel_type: FuelType):
    async with AsyncSessionLocal() as db:
        price = await get_latest_fresh_price(db, station.id, fuel_type)
        price_text = f"{price.price:.2f} ₽" if price and price.price is not None else "уточняется на стеле"
        dist = haversine_distance(lat, lon, station.latitude, station.longitude)
        time_min = max(1, round(dist / 40 * 60))

        station_name = html.escape(station.name)
        station_address = html.escape(station.address or "адрес не указан")

        nav_url = f"https://yandex.ru/maps/?pt={station.longitude},{station.latitude}&z=15"

        await message.answer(
            f"🚗 <b>Ближайшая АЗС с гарантированным топливом:</b>\n\n"
            f"⛽ <b>{station_name}</b>\n"
            f"📍 Адрес: <code>{station_address}</code>\n"
            f"💰 Цена ({fuel_type.value}): <b>{price_text}</b>\n"
            f"📏 Расстояние: <b>{dist:.1f} км</b> (~{time_min} мин)\n\n"
            f"🗺 <a href='{nav_url}'>Проложить маршрут в Яндекс.Картах</a>\n\n"
            f"<i>Доступ активен в течение 2 часов. Удачной дороги!</i>",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML"
        )
