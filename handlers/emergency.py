# handlers/emergency.py — ПОЛНАЯ ВЕРСИЯ С СОХРАНЁННОЙ ГЕОЛОКАЦИЕЙ
import logging
import html
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, LabeledPrice
from datetime import datetime, timezone

from database.session import AsyncSessionLocal
from database.crud import get_user, get_city_by_id, find_nearest_green_station, get_latest_fresh_price, save_user_location
from database.models import FuelType
from utils.helpers import haversine_distance
from utils.geocoder import geocode_address
from services.subscription import check_pro
from keyboards.reply import main_menu_keyboard
from keyboards.inline import pro_purchase_keyboard, emergency_payment_keyboard
from config import settings

logger = logging.getLogger(__name__)
router = Router()

class EmergencyStates(StatesGroup):
    waiting_address = State()
    waiting_payment = State()

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

        # Если есть сохранённая геолокация — предложим использовать её
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

    # Если нет сохранённой геолокации — запрашиваем
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Отправить местоположение", request_location=True)],
            [KeyboardButton(text="✏️ Ввести адрес вручную")],
            [KeyboardButton(text="❌ Отмена")],
        ],
        resize_keyboard=True
    )
    await state.set_state(EmergencyStates.waiting_address)
    await message.answer(
        "🚨 Бензин на нуле? Я проверю, есть ли поблизости АЗС с топливом.\n\n"
        "📎 Отправь своё местоположение или напиши адрес.",
        reply_markup=kb
    )

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
    # Сохраняем геолокацию в профиль
    async with AsyncSessionLocal() as db:
        await save_user_location(db, message.from_user.id, lat, lon)
    await check_availability_and_offer(message, state, lat, lon)

@router.message(EmergencyStates.waiting_address, F.text)
async def emergency_address(message: types.Message, state: FSMContext):
    address = message.text.strip()
    if not address:
        await message.answer("❌ Адрес не может быть пустым. Попробуйте снова.")
        return

    if address.lower() == "❌ отмена":
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

async def check_availability_and_offer(message: types.Message, state: FSMContext, lat: float, lon: float):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        city = await get_city_by_id(db, user.city_id)
        if not city:
            await message.answer("❌ Город не найден. Сначала выберите город.", reply_markup=main_menu_keyboard())
            await state.clear()
            return

        is_pro = await check_pro(message.from_user.id)

        station = await find_nearest_green_station(db, city.id, lat, lon, radius_km=5.0)
        if not station:
            if is_pro:
                station = await find_nearest_green_station(db, city.id, lat, lon, radius_km=10.0)
                if not station:
                    await message.answer(
                        "❌ В радиусе 10 км нет АЗС с подтверждённым наличием АИ-95.\n"
                        "Попробуйте обычный поиск.",
                        reply_markup=main_menu_keyboard()
                    )
                    await state.clear()
                    return
                await show_result(message, station, lat, lon)
                await state.clear()
                return
            else:
                await message.answer(
                    "❌ В радиусе 5 км нет АЗС с подтверждённым наличием АИ-95.\n\n"
                    "🚀 Расширьте радиус до 10 км с PRO-подпиской и найдите заправку!",
                    reply_markup=pro_purchase_keyboard()
                )
                await state.clear()
                return

        await state.update_data(station_id=station.id, user_lat=lat, user_lon=lon)

        dist = haversine_distance(lat, lon, station.latitude, station.longitude)
        dist_text = f"{dist:.1f} км"

        if is_pro:
            await show_result(message, station, lat, lon)
            await state.clear()
        else:
            await state.set_state(EmergencyStates.waiting_payment)
            await message.answer(
                f"🔍 Проверяю данные…\n\n"
                f"✅ Рядом с вами есть АЗС с АИ‑95. Ближайшая — в {dist_text}.\n\n"
                f"Чтобы увидеть название, адрес и проложить маршрут:\n"
                f"💳 Оплатить 50 ₽ (карта)\n"
                f"⭐ Оплатить 50 Stars\n"
                f"🔥 Купить PRO (99 ₽/мес) — и получать экстренные поиски бесплатно всегда",
                reply_markup=emergency_payment_keyboard()
            )

# ---------- ОПЛАТА ----------
@router.callback_query(F.data == "pay_emergency_rub")
async def pay_emergency_rub(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    order_id = f"emergency_rub_{user_id}_{int(datetime.now().timestamp())}"
    prices = [LabeledPrice(label="🚨 Экстренный поиск АЗС", amount=5000)]
    try:
        await callback.message.answer_invoice(
            title="🚨 Экстренный поиск АЗС",
            description="Найдём ближайшую АЗС с топливом за 10 секунд",
            provider_token=settings.PAYMENT_PROVIDER_TOKEN,
            currency="RUB",
            prices=prices,
            start_parameter="emergency",
            payload=order_id,
        )
    except Exception as e:
        logger.error(f"Ошибка создания инвойса: {e}")
        await callback.message.answer("❌ Не удалось создать платёж. Попробуйте позже.")

@router.callback_query(F.data == "pay_emergency_stars")
async def pay_emergency_stars(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    order_id = f"emergency_stars_{user_id}_{int(datetime.now().timestamp())}"
    prices = [LabeledPrice(label="🚨 Экстренный поиск АЗС", amount=50)]
    try:
        await callback.message.answer_invoice(
            title="🚨 Экстренный поиск АЗС",
            description="Найдём ближайшую АЗС с топливом за 10 секунд",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="emergency_stars",
            payload=order_id,
        )
    except Exception as e:
        logger.error(f"Ошибка создания Stars инвойса: {e}")
        await callback.message.answer("❌ Не удалось создать платёж Stars. Попробуйте рублёвую оплату.")

# ---------- ОТОБРАЖЕНИЕ РЕЗУЛЬТАТА ----------
async def show_result(message: types.Message, station, lat: float, lon: float):
    async with AsyncSessionLocal() as db:
        price = await get_latest_fresh_price(db, station.id, FuelType.AI_95)
        price_text = f"{price.price:.2f} ₽" if price and price.price is not None else "неизвестна"
        dist = haversine_distance(lat, lon, station.latitude, station.longitude)
        time_min = round(dist / 40 * 60)
        station_name = html.escape(station.name)
        station_address = html.escape(station.address or "адрес не указан")

        await message.answer(
            f"🚗 Отлично! Вот ближайшая АЗС с топливом:\n\n"
            f"📍 <b>{station_name}</b>\n"
            f"Адрес: {station_address}\n"
            f"⛽ Цена: {price_text}\n"
            f"📏 {dist:.1f} км, ~{time_min} мин в пути\n"
            f"\n🗺 <a href='https://yandex.ru/maps/?pt={station.longitude},{station.latitude}&z=15'>Открыть маршрут</a>\n\n"
            f"Спасибо, что пользуетесь BinzoLife!",
            reply_markup=main_menu_keyboard(),
            parse_mode="HTML"
        )
