import logging
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

from database.session import AsyncSessionLocal
from database.crud import get_user, get_city_by_id, find_nearest_green_station, get_latest_fresh_price
from database.models import FuelType
from utils.helpers import haversine_distance
from utils.geocoder import geocode_address
from services.subscription import check_pro
from keyboards.reply import main_menu_keyboard
from keyboards.inline import pro_purchase_keyboard

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
        "📎 Отправь своё местоположение или напиши адрес (например, «ТРЦ Планета, Красноярск»).",
        reply_markup=kb
    )

@router.message(EmergencyStates.waiting_address, F.location)
async def emergency_location(message: types.Message, state: FSMContext):
    lat = message.location.latitude
    lon = message.location.longitude
    await state.update_data(lat=lat, lon=lon)
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
            "Попробуйте отправить геолокацию через кнопку ниже.",
            reply_markup=ReplyKeyboardMarkup(
                keyboard=[[KeyboardButton(text="📍 Отправить местоположение", request_location=True)]],
                resize_keyboard=True
            )
        )
        return

    lat, lon = coords
    await state.update_data(lat=lat, lon=lon)
    await check_availability_and_offer(message, state, lat, lon)

@router.message(EmergencyStates.waiting_address, F.text == "❌ Отмена")
async def emergency_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Поиск отменён.", reply_markup=main_menu_keyboard())

async def check_availability_and_offer(message: types.Message, state: FSMContext, lat: float, lon: float):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        city = await get_city_by_id(db, user.city_id)
        if not city:
            await message.answer("❌ Город не найден. Сначала выберите город.", reply_markup=main_menu_keyboard())
            await state.clear()
            return

        is_pro = await check_pro(message.from_user.id)

        # Ищем станции в радиусе 5 км
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
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💳 Оплатить 50 ₽", callback_data="pay_emergency_rub")],
                    [InlineKeyboardButton(text="⭐ Оплатить 50 Stars", callback_data="pay_emergency_stars")],
                    [InlineKeyboardButton(text="🔥 Купить PRO", callback_data="buy_pro")]
                ])
            )

@router.callback_query(F.data == "pay_emergency_rub")
async def pay_emergency_rub(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    from handlers.payments import send_invoice
    await send_invoice(callback.message, amount=50, payload="emergency_search", description="Экстренный поиск АЗС")

@router.callback_query(F.data == "pay_emergency_stars")
async def pay_emergency_stars(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Оплата Stars пока в разработке. Используйте рублёвую оплату.", show_alert=True)

async def show_result(message: types.Message, station, lat: float, lon: float):
    async with AsyncSessionLocal() as db:
        price = await get_latest_fresh_price(db, station.id, FuelType.AI_95)
        price_text = f"{price.price:.2f} ₽" if price else "неизвестна"
        dist = haversine_distance(lat, lon, station.latitude, station.longitude)
        time_min = round(dist / 40 * 60)

        await message.answer(
            f"🚗 Отлично! Вот ближайшая АЗС с топливом:\n\n"
            f"📍 <b>{station.name}</b>\n"
            f"Адрес: {station.address}\n"
            f"⛽ Цена: {price_text}\n"
            f"📏 {dist:.1f} км, ~{time_min} мин в пути\n"
            f"\n🗺 <a href='https://yandex.ru/maps/?pt={station.longitude},{station.latitude}&z=15'>Открыть маршрут</a>\n\n"
            f"Спасибо, что пользуетесь BinzoLife! Сохраните контакт на случай, если бензин снова закончится.",
            reply_markup=main_menu_keyboard()
        )
