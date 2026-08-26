import html
import logging
import io
import qrcode
from datetime import datetime, timedelta, timezone, date
from PIL import Image, ImageDraw, ImageFont
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from sqlalchemy import select, func

from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_user, log_action, get_city_by_id, get_station_by_id,
    get_latest_price, create_notification, save_price,
    get_latest_fresh_price, get_avg_price_30d, get_min_price_30d, get_max_price_30d,
    set_first_search, get_active_notifications_for_user,
    activate_trial, increment_station_views, get_referral_link,
    save_availability_report_with_consensus
)
from database.models import FuelType, AvailabilityStatus, SourceType, Station, FuelPrice, AvailabilityReport, UserAction
from services.rating import calculate_rating
from services.subscription import check_pro
from services.graphics import generate_price_graph
from utils.helpers import status_emoji, format_time_ago, haversine_distance
from keyboards.reply import main_menu_keyboard, fuel_choice_keyboard
from keyboards.inline import sort_choice_keyboard, station_action_keyboard, pro_purchase_keyboard

logger = logging.getLogger(__name__)
router = Router()

class FindStates(StatesGroup):
    choosing_fuel = State()
    choosing_sort = State()

class ReportPriceStates(StatesGroup):
    waiting_price = State()

# ---------- Генерация картинки для шеринга ----------
async def generate_share_image(name: str, price: float, status: str, address: str, ref_link: str) -> bytes:
    try:
        img = Image.new('RGB', (800, 400), color='#1a1a2e')
        draw = ImageDraw.Draw(img)
        try:
            font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 32)
            font_text = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
        except:
            font_title = ImageFont.load_default()
            font_text = ImageFont.load_default()

        draw.text((50, 50), f"⛽ {name}", fill='white', font=font_title)
        draw.text((50, 120), f"Цена: {price} ₽/л", fill='#4caf50', font=font_text)
        draw.text((50, 170), f"Наличие: {status}", fill='#ffeb3b', font=font_text)
        draw.text((50, 220), f"Адрес: {address[:50]}", fill='#bbdefb', font=font_text)

        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(ref_link)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white")
        qr_img = qr_img.resize((150, 150))
        img.paste(qr_img, (600, 200))

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Ошибка генерации картинки: {e}")
        return None

# ---------- Старт поиска ----------
@router.message(F.text == "⛽ Найти заправку")
async def start_find(message: types.Message, state: FSMContext):
    await state.set_state(FindStates.choosing_fuel)
    await message.answer("Выберите вид топлива:", reply_markup=fuel_choice_keyboard())

@router.message(FindStates.choosing_fuel, F.text == "⛽ АИ-95")
async def choose_fuel(message: types.Message, state: FSMContext):
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            user = await create_user(db, message.from_user.id, message.from_user.username)
            logger.info(f"Создан новый пользователь {user.telegram_id} в процессе выбора топлива")

        if not user.city_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 Перейти в профиль", callback_data="go_profile")]
            ])
            await message.answer(
                "❌ Город не выбран. Пожалуйста, установите город в профиле.",
                reply_markup=kb
            )
            await state.clear()
            return

        city = await get_city_by_id(db, user.city_id)
        if not city or city.latitude is None or city.longitude is None:
            await message.answer(
                "❌ У выбранного города не заданы координаты. Обратитесь к администратору для установки координат через /set_city_coords.",
                reply_markup=main_menu_keyboard()
            )
            await state.clear()
            return

        await state.update_data(city_id=city.id, lat=city.latitude, lon=city.longitude, fuel_type=FuelType.AI_95)

    await state.set_state(FindStates.choosing_sort)
    await message.answer(
        "Как отсортировать результаты?\n\n"
        "🔥 По рейтингу (баланс цены и наличия)\n"
        "💰 По минимальной цене",
        reply_markup=sort_choice_keyboard()
    )

@router.message(FindStates.choosing_fuel, F.text == "👤 Профиль")
async def profile_from_choosing_fuel(message: types.Message, state: FSMContext):
    await state.clear()
    from handlers.profile import show_profile
    await show_profile(message)

@router.message(FindStates.choosing_fuel)
async def handle_unknown_in_choosing_fuel(message: types.Message, state: FSMContext):
    await message.answer("Пожалуйста, воспользуйтесь кнопками ниже.", reply_markup=fuel_choice_keyboard())

# ---------- Обработчики выбора сортировки ----------
@router.callback_query(F.data == "sort_rating")
async def sort_rating(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(sort_mode="rating")
    await perform_search(callback.message, state)

@router.callback_query(F.data == "sort_price")
async def sort_price(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(sort_mode="price")
    await perform_search(callback.message, state)

# ---------- Основная функция поиска ----------
async def perform_search(message: types.Message, state: FSMContext):
    try:
        data = await state.get_data()
        city_id = data.get("city_id")
        fuel_type = data.get("fuel_type", FuelType.AI_95)
        sort_mode = data.get("sort_mode", "rating")

        if not city_id:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 Перейти в профиль", callback_data="go_profile")]
            ])
            await message.answer("❌ Город не выбран. Пожалуйста, установите город в профиле.", reply_markup=kb)
            await state.clear()
            return

        async with AsyncSessionLocal() as db:
            user = await get_user(db, message.from_user.id)
            if not user:
                user = await create_user(db, message.from_user.id, message.from_user.username)
                logger.info(f"Создан новый пользователь {user.telegram_id} во время поиска")

            if not user.city_id and city_id:
                city = await get_city_by_id(db, city_id)
                if city:
                    user.city_id = city_id
                    await db.commit()
                else:
                    await message.answer("❌ Город не найден. Пожалуйста, выберите город в профиле.")
                    await state.clear()
                    return

            if not user.city_id:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👤 Перейти в профиль", callback_data="go_profile")]
                ])
                await message.answer(
                    "❌ Город не выбран. Пожалуйста, установите город в профиле.",
                    reply_markup=kb
                )
                await state.clear()
                return

            city = await get_city_by_id(db, user.city_id)
            if not city or city.latitude is None or city.longitude is None:
                await message.answer(
                    "❌ У выбранного города не заданы координаты. Обратитесь к администратору.",
                    reply_markup=main_menu_keyboard()
                )
                await state.clear()
                return

            city_lat = city.latitude
            city_lon = city.longitude

            await set_first_search(db, user.id)
            is_pro = await check_pro(user.telegram_id)

            stations = await db.execute(
                select(Station).where(Station.city_id == city.id, Station.is_active == True)
            )
            stations = stations.scalars().all()
            if not stations:
                await message.answer("В этом городе пока нет АЗС.", reply_markup=main_menu_keyboard())
                await state.clear()
                return

            station_ids = [s.id for s in stations]

            price_subq = (
                select(FuelPrice.station_id, FuelPrice.fuel_type, func.max(FuelPrice.recorded_at).label("max_date"))
                .where(FuelPrice.station_id.in_(station_ids), FuelPrice.fuel_type == fuel_type, FuelPrice.is_fresh == True)
                .group_by(FuelPrice.station_id, FuelPrice.fuel_type)
                .subquery()
            )
            price_stmt = (
                select(FuelPrice)
                .join(price_subq,
                      (FuelPrice.station_id == price_subq.c.station_id) &
                      (FuelPrice.fuel_type == price_subq.c.fuel_type) &
                      (FuelPrice.recorded_at == price_subq.c.max_date))
            )
            price_result = await db.execute(price_stmt)
            prices = {p.station_id: p for p in price_result.scalars().all()}

            avail_subq = (
                select(AvailabilityReport.station_id, AvailabilityReport.fuel_type, func.max(AvailabilityReport.recorded_at).label("max_date"))
                .where(AvailabilityReport.station_id.in_(station_ids), AvailabilityReport.fuel_type == fuel_type, AvailabilityReport.is_fresh == True)
                .group_by(AvailabilityReport.station_id, AvailabilityReport.fuel_type)
                .subquery()
            )
            avail_stmt = (
                select(AvailabilityReport)
                .join(avail_subq,
                      (AvailabilityReport.station_id == avail_subq.c.station_id) &
                      (AvailabilityReport.fuel_type == avail_subq.c.fuel_type) &
                      (AvailabilityReport.recorded_at == avail_subq.c.max_date))
            )
            avail_result = await db.execute(avail_stmt)
            avails = {a.station_id: a for a in avail_result.scalars().all()}

            avg_price = await get_avg_price_30d(db, city.id, fuel_type) or 0
            min_price = await get_min_price_30d(db, city.id, fuel_type) or 0
            max_price = await get_max_price_30d(db, city.id, fuel_type) or 0

            results = []
            for station in stations:
                price_rec = prices.get(station.id)
                if not price_rec:
                    continue
                avail_rec = avails.get(station.id)
                rating_data = calculate_rating(
                    station=station,
                    price_record=price_rec,
                    availability_record=avail_rec,
                    avg_price_30d=avg_price or price_rec.price,
                    min_price_30d=min_price or price_rec.price,
                    max_price_30d=max_price or price_rec.price
                )
                dist = haversine_distance(city_lat, city_lon, station.latitude, station.longitude)
                results.append({
                    "station": station,
                    "price": price_rec.price,
                    "price_time": price_rec.recorded_at,
                    "availability": avail_rec.status if avail_rec else AvailabilityStatus.GRAY,
                    "availability_time": avail_rec.recorded_at if avail_rec else None,
                    "distance_km": dist,
                    "rating": rating_data["rating"],
                    "explanation": rating_data["explanation"],
                    "price_diff": rating_data.get("price_diff", 0),
                    "avg_price": rating_data.get("avg_price", 0),
                })

            if not results:
                await message.answer("Не найдено АЗС с актуальными ценами. Попробуйте позже.")
                await state.clear()
                return

            if sort_mode == "price":
                results.sort(key=lambda x: x["price"])
            else:
                results.sort(key=lambda x: x["rating"], reverse=True)

            await state.update_data(all_results=results, current_index=0, is_pro=is_pro)

            await show_station_card(message, results[0], 0, len(results), is_pro, state)

            await log_action(db, user.id, "search_result")

            if user and not user.is_pro and not user.trial_used:
                await activate_trial(db, user.id)
                await message.answer(
                    "🎁 Вам активирован 3-дневный пробный период PRO!\n"
                    "Теперь вы можете пользоваться уведомлениями, графиками и экстренным поиском бесплатно.\n"
                    "После окончания триала вы сможете оформить подписку за 99 ₽/мес."
                )

    except Exception as e:
        logger.error(f"Ошибка в perform_search: {e}", exc_info=True)
        await message.answer("⚠️ Произошла ошибка при поиске. Попробуйте позже.")
        await state.clear()

# ---------- Функция отображения карточки (исправлена) ----------
async def show_station_card(message: types.Message, result: dict, index: int, total: int, is_pro: bool, state: FSMContext):
    try:
        if not result:
            await message.answer("Ошибка: нет данных для отображения.")
            return

        station = result.get("station")
        if not station:
            await message.answer("Ошибка: данные о станции отсутствуют.")
            return

        price = result.get("price", 0.0)
        price_time = result.get("price_time")
        availability = result.get("availability", AvailabilityStatus.GRAY)
        availability_time = result.get("availability_time")
        distance_km = result.get("distance_km", 0.0)
        rating = result.get("rating", 0)
        explanation = result.get("explanation", "")
        price_diff = result.get("price_diff", 0)
        avg_price = result.get("avg_price", 0)

        station_name = html.escape(station.name)
        station_address = html.escape(station.address) if station.address else "адрес не указан"

        # Расстояние и время
        if distance_km > 0 and distance_km < 1000:  # реалистичное расстояние
            distance_text = f"{distance_km:.1f} км"
            time_min = round(distance_km / 40 * 60)
            time_text = f"~{time_min} мин"
        else:
            distance_text = "расстояние неизвестно"
            time_text = ""

        status_text = status_emoji(availability.value if availability else "GRAY")
        status_time = format_time_ago(availability_time) if availability_time else "неизвестно"
        price_time_str = format_time_ago(price_time) if price_time else "неизвестно"

        stars = round(rating / 20, 1) if rating else 0
        stars_display = f"⭐ {stars} ({rating}/100)"

        # Статус наличия
        if availability == AvailabilityStatus.GRAY:
            status_display = "⚪ Наличие: неизвестно\n🔄 Обновите данные через кнопку «Сообщить цену»"
        else:
            status_display = f"{status_text} Наличие: {availability.value} ({status_time})"

        # Счётчик просмотров
        async with AsyncSessionLocal() as db:
            views = await increment_station_views(db, station.id)
            views_text = f"🔥 Эту АЗС сегодня выбрали {views} водителей.\n" if views else ""

        # Формируем текст карточки
        text = (
            f"⛽ {station_name} {stars_display}\n\n"
            f"📍 {station_address}"
        )
        if distance_text != "расстояние неизвестно":
            text += f" · {distance_text}"
            if time_text:
                text += f"\n🚗 {time_text} на машине"
        else:
            text += f"\n📍 расстояние не определено (установите координаты города)"

        text += f"\n\n💰 Цена: {price:.2f} ₽/л"
        if avg_price and avg_price > 0 and abs(price_diff) > 0.01:
            if price_diff > 0:
                text += f" (на {price_diff:.2f} ₽ дешевле средней по городу)"
            elif price_diff < 0:
                text += f" (на {abs(price_diff):.2f} ₽ дороже средней по городу)"
        else:
            text += " (цена близка к средней по городу)"

        text += f"\n🕒 Обновлено: {price_time_str}"
        text += f"\n{views_text}"
        text += f"\n{status_display}"

        if not is_pro:
            text += "\n\n—\n⚠️ Бесплатная версия показывает данные с задержкой до 2 часов.\n"
            text += "Чтобы получать уведомления о появлении топлива и снижении цены в реальном времени — подключи PRO за 99 ₽/мес."

        keyboard = station_action_keyboard(
            station_id=station.id,
            price=price,
            availability=availability,
            lat=station.latitude,
            lon=station.longitude,
            city_id=station.city_id,
            is_pro=is_pro,
            index=index,
            total=total
        )

        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Ошибка при отправке карточки: {e}", exc_info=True)
        await message.answer("Произошла ошибка при отправке карточки. Попробуйте позже.")

# ---------- Остальные обработчики (без изменений) ----------
@router.callback_query(lambda c: c.data.startswith("more_"))
async def show_more(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        data = await state.get_data()
        all_results = data.get("all_results", [])
        current_index = data.get("current_index", 0)
        is_pro = data.get("is_pro", False)

        if not all_results or current_index + 1 >= len(all_results):
            await callback.message.answer("Это все доступные варианты.")
            return

        next_index = current_index + 1
        more_results = all_results[next_index:next_index+2]

        text = "Вот ещё две АЗС с хорошими ценами:\n\n"
        for i, res in enumerate(more_results, start=next_index+1):
            station = res.get("station")
            if not station:
                continue
            price = res.get("price", 0.0)
            distance = res.get("distance_km", 0.0)
            availability = res.get("availability", AvailabilityStatus.GRAY)
            status = availability.value if availability else "GRAY"
            station_name = html.escape(station.name)

            if station.latitude and station.longitude:
                map_link = f"<a href='https://yandex.ru/maps/?pt={station.longitude},{station.latitude}&z=15'>Маршрут</a>"
            else:
                map_link = "Координаты отсутствуют"

            dist_text = f"{distance:.1f} км" if distance > 0 else "расстояние неизвестно"
            text += (
                f"{i}. {station_name} — {price:.2f} ₽/л, {status}, {dist_text}\n"
                f"   🗺 {map_link}\n\n"
            )

        await state.update_data(current_index=next_index+1)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад к лучшей", callback_data="back_to_best")],
            [InlineKeyboardButton(text="🔄 Найти заново", callback_data="restart_search")]
        ])
        await callback.message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в show_more: {e}", exc_info=True)
        await callback.message.answer("Произошла ошибка. Попробуйте начать поиск заново.")

@router.callback_query(F.data == "back_to_best")
async def back_to_best(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        data = await state.get_data()
        all_results = data.get("all_results", [])
        is_pro = data.get("is_pro", False)
        if all_results:
            await show_station_card(callback.message, all_results[0], 0, len(all_results), is_pro, state)
        else:
            await callback.message.answer("Нет сохранённых результатов.", reply_markup=main_menu_keyboard())
            await state.clear()
    except Exception as e:
        logger.error(f"Ошибка в back_to_best: {e}", exc_info=True)
        await callback.message.answer("Произошла ошибка. Попробуйте начать поиск заново.")
        await state.clear()

@router.callback_query(F.data == "restart_search")
async def restart_search(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await start_find(callback.message, state)

@router.callback_query(F.data == "go_profile")
async def go_profile_callback(callback: types.CallbackQuery):
    await callback.answer()
    from handlers.profile import show_profile
    await show_profile(callback.message)

# ---------- Следить за ценой (PRO) ----------
@router.callback_query(lambda c: c.data.startswith("follow_"))
async def follow_price(callback: types.CallbackQuery):
    logger.info(f"[CALLBACK] follow_ вызван: {callback.data}")
    await callback.answer()
    try:
        station_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError) as e:
        logger.error(f"Ошибка парсинга station_id из {callback.data}: {e}")
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return

    if not await check_pro(callback.from_user.id):
        await callback.message.answer(
            "⛔ <b>Уведомления о снижении цены доступны только для PRO-подписчиков</b>\n\n"
            "💎 Оформите PRO за 99 ₽/месяц и получайте оповещения:\n"
            "• Когда цена упадёт ниже заданного уровня\n"
            "• Когда на вашей АЗС появится бензин\n"
            "• Графики цен и аналитику\n\n"
            "Нажмите на кнопку «💎 PRO» в главном меню, чтобы оплатить.",
            reply_markup=pro_purchase_keyboard()
        )
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.message.answer("Сначала выполните /start")
            return
        station = await get_station_by_id(db, station_id)
        if not station:
            await callback.message.answer("АЗС не найдена.")
            return

        existing = await get_active_notifications_for_user(db, user.id)
        for n in existing:
            if n.station_id == station_id and n.fuel_type == FuelType.AI_95 and n.notify_on_low_price:
                await callback.message.answer("Вы уже подписаны на эту АЗС на снижение цены.")
                return

        latest_price = await get_latest_fresh_price(db, station_id, FuelType.AI_95)
        if not latest_price:
            await callback.message.answer("Не удалось получить текущую цену.")
            return
        target_price = round(latest_price.price - 0.5, 2)
        if target_price < 0:
            target_price = 0

        await create_notification(
            db,
            user_id=user.id,
            fuel_type=FuelType.AI_95,
            station_id=station_id,
            target_price=target_price,
            notify_on_low_price=True
        )
        await callback.message.answer(
            f"✅ Подписка на цену на АЗС <b>{station.name}</b> активирована.\n"
            f"Я сообщу, когда цена станет ≤ {target_price} ₽.\n"
            f"(Отписаться можно в разделе «Мои уведомления».)"
        )

# ---------- Уведомления о появлении (PRO) ----------
@router.callback_query(lambda c: c.data.startswith("alert_avail_"))
async def subscribe_availability(callback: types.CallbackQuery):
    logger.info(f"[CALLBACK] alert_avail_ вызван: {callback.data}")
    await callback.answer()
    try:
        station_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError) as e:
        logger.error(f"Ошибка парсинга station_id из {callback.data}: {e}")
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return

    if not await check_pro(callback.from_user.id):
        await callback.message.answer(
            "⛔ <b>Уведомления о появлении топлива доступны только для PRO-подписчиков</b>\n\n"
            "💎 Оформите PRO за 99 ₽/месяц и получайте оповещения:\n"
            "• Когда на вашей АЗС появится бензин\n"
            "• Когда цена упадёт ниже целевого уровня\n\n"
            "Нажмите на кнопку «💎 PRO» в главном меню, чтобы оплатить.",
            reply_markup=pro_purchase_keyboard()
        )
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.message.answer("Сначала /start")
            return
        station = await get_station_by_id(db, station_id)
        if not station:
            await callback.answer("АЗС не найдена")
            return

        existing = await get_active_notifications_for_user(db, user.id)
        for n in existing:
            if n.station_id == station_id and n.fuel_type == FuelType.AI_95 and n.notify_on_availability:
                await callback.message.answer("Вы уже подписаны на уведомления о появлении на этой АЗС.")
                return

        await create_notification(
            db,
            user_id=user.id,
            fuel_type=FuelType.AI_95,
            station_id=station_id,
            notify_on_availability=True
        )
    await callback.answer("Вы подписаны на уведомления о появлении топлива на этой АЗС")
    await callback.message.answer(f"🔔 Вы будете получать уведомления, когда на {station.name} появится АИ-95.")

# ---------- Сообщить цену ----------
@router.callback_query(lambda c: c.data.startswith("report_price_"))
async def start_report_price(callback: types.CallbackQuery, state: FSMContext):
    logger.info(f"[CALLBACK] report_price_ вызван: {callback.data}")
    await callback.answer()
    try:
        station_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError) as e:
        logger.error(f"Ошибка парсинга station_id из {callback.data}: {e}")
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return
    await state.update_data(report_station_id=station_id)
    await state.set_state(ReportPriceStates.waiting_price)
    await callback.message.answer(
        "✏️ Введите актуальную цену на этой АЗС (в рублях за литр, например, 68.50):\n\n"
        "(Если хотите сообщить статус наличия, добавьте через пробел: цена статус, например: 68.50 GREEN)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
        ])
    )

@router.message(ReportPriceStates.waiting_price, F.text)
async def process_report_price(message: types.Message, state: FSMContext):
    parts = message.text.strip().split()
    try:
        price = float(parts[0].replace(',', '.'))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Пожалуйста, введите положительное число (например, 68.50).")
        return

    status = None
    if len(parts) > 1:
        status_str = parts[1].upper()
        if status_str in ["GREEN", "YELLOW", "RED", "GRAY"]:
            status = AvailabilityStatus[status_str]

    data = await state.get_data()
    station_id = data.get("report_station_id")
    if not station_id:
        await message.answer("Ошибка: АЗС не найдена. Попробуйте заново.")
        await state.clear()
        return

    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала /start")
            await state.clear()
            return
        station = await get_station_by_id(db, station_id)
        if not station:
            await message.answer("АЗС не найдена.")
            await state.clear()
            return

        await save_price(
            db,
            station_id=station_id,
            fuel_type=FuelType.AI_95,
            price=price,
            source=SourceType.USER,
            confidence=0.6
        )
        if status:
            await save_availability_report_with_consensus(
                db, station_id, FuelType.AI_95, status, SourceType.USER, confidence=0.6, user_id=user.id
            )
        user.reputation += 1
        await db.commit()

    from handlers.profile import get_user_level
    level_name, _, rep_to_next, _ = get_user_level(user.reputation)
    await message.answer(
        f"✅ Спасибо! Ваша цена сохранена. Вы получили +1 репутацию.\n"
        f"Текущий уровень: {level_name} (до следующего уровня осталось {rep_to_next} баллов).",
        reply_markup=main_menu_keyboard()
    )
    await state.clear()

@router.callback_query(F.data == "cancel_report")
async def cancel_report(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Отмена. Главное меню:", reply_markup=main_menu_keyboard())

# ---------- График цен (PRO) ----------
@router.callback_query(lambda c: c.data.startswith("graph_"))
async def show_graph(callback: types.CallbackQuery):
    logger.info(f"[CALLBACK] graph_ вызван: {callback.data}")
    await callback.answer()
    try:
        station_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError) as e:
        logger.error(f"Ошибка парсинга station_id из {callback.data}: {e}")
        await callback.message.answer("Произошла ошибка. Попробуйте ещё раз.")
        return

    if not await check_pro(callback.from_user.id):
        await callback.message.answer(
            "⛔ <b>График цен доступен только для PRO-подписчиков</b>\n\n"
            "💎 Оформите PRO за 99 ₽/месяц и получите:\n"
            "• Уведомления о снижении цены\n"
            "• Оповещения о появлении топлива\n"
            "• Графики цен на АЗС\n\n"
            "Нажмите на кнопку «💎 PRO» в главном меню, чтобы оплатить.",
            reply_markup=pro_purchase_keyboard()
        )
        return
    logger.info(f"Генерируем график для station_id={station_id}")
    try:
        graph_bytes = await generate_price_graph(station_id, FuelType.AI_95, days=30)
        if graph_bytes:
            await callback.message.answer_photo(
                photo=BufferedInputFile(graph_bytes, filename="price.png"),
                caption="📊 Динамика цены за 30 дней"
            )
            logger.info("График отправлен")
        else:
            await callback.message.answer(
                "📊 Недостаточно данных для построения графика.\n"
                "Для этой АЗС пока нет истории цен за 30 дней.\n"
                "Попробуйте позже, когда накопится больше данных."
            )
            logger.info("Нет данных для графика")
    except Exception as e:
        logger.error(f"Ошибка при генерации графика: {e}", exc_info=True)
        await callback.message.answer(f"❌ Ошибка при генерации графика: {e}")

# ---------- Поделиться (вирусная механика) ----------
@router.callback_query(lambda c: c.data.startswith("share_"))
async def share_station(callback: types.CallbackQuery):
    station_id = int(callback.data.split("_")[1])
    async with AsyncSessionLocal() as db:
        station = await get_station_by_id(db, station_id)
        if not station:
            await callback.answer("АЗС не найдена")
            return
        price_record = await get_latest_price(db, station_id, FuelType.AI_95)
        price = price_record.price if price_record else "неизвестна"
        status_record = await get_latest_availability(db, station_id, FuelType.AI_95)
        status = status_record.status.value if status_record else "неизвестно"
        user = await get_user(db, callback.from_user.id)
        if not user:
            await callback.answer("Сначала /start")
            return

        ref_link = await get_referral_link(db, user)
        share_text = "Экономлю с BinzoLife — присоединяйся!"

        img = await generate_share_image(station.name, price, status, station.address, ref_link)
        if img:
            await callback.message.answer_photo(
                photo=BufferedInputFile(img, filename="share.png"),
                caption=f"📤 Поделитесь этой АЗС с друзьями!\n\n{share_text}\n\nВаша реферальная ссылка: {ref_link}"
            )
        else:
            await callback.message.answer(
                f"📤 Поделитесь этой АЗС с друзьями!\n\n{share_text}\n\nВаша реферальная ссылка: {ref_link}"
            )

        today = date.today()
        share_today = await db.execute(
            select(UserAction)
            .where(
                UserAction.user_id == user.id,
                UserAction.action == "share",
                func.date(UserAction.recorded_at) == today
            )
        )
        if not share_today.scalar_one_or_none():
            user.reputation += 1
            action = UserAction(user_id=user.id, action="share", station_id=station_id)
            db.add(action)
            await db.commit()
            await callback.message.answer("✅ Вы поделились! +1 репутация (можно 1 раз в день).\n\nА если ваш друг перейдёт по ссылке и сделает первый поиск — вы получите 3 дня PRO бесплатно.")
        else:
            await callback.message.answer("ℹ️ Вы уже получали репутацию за репост сегодня.")
