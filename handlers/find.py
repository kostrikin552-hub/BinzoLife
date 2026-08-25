import logging
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from sqlalchemy import select, func
from datetime import datetime, timedelta, timezone
from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_user, log_action, get_city_by_id, get_station_by_id,
    get_latest_price, create_notification, save_price,
    get_latest_fresh_price, get_avg_price_30d, get_min_price_30d, get_max_price_30d,
    set_first_search, get_active_notifications_for_user
)
from database.models import FuelType, AvailabilityStatus, SourceType, Station, FuelPrice, AvailabilityReport
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

# ---------- Старт поиска ----------
@router.message(F.text == "⛽ Найти заправку")
async def start_find(message: types.Message, state: FSMContext):
    await state.set_state(FindStates.choosing_fuel)
    await message.answer("Выберите вид топлива:", reply_markup=fuel_choice_keyboard())

# Обработчик любых сообщений в состоянии выбора топлива (кроме кнопок)
@router.message(FindStates.choosing_fuel)
async def handle_unknown_in_choosing_fuel(message: types.Message, state: FSMContext):
    await message.answer("Пожалуйста, воспользуйтесь кнопками ниже.", reply_markup=fuel_choice_keyboard())

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
                "❌ У выбранного города не заданы координаты. Обратитесь к администратору.",
                reply_markup=main_menu_keyboard()
            )
            await state.clear()
            return

        await state.update_data(city_id=city.id, lat=city.latitude, lon=city.longitude, fuel_type=FuelType.AI_95)

    await state.set_state(FindStates.choosing_sort)
    await message.answer(
        "⛽ Как будем выбирать лучшую АЗС?\n\n"
        "🔥 По рейтингу — идеальный баланс цены, наличия и расстояния (рекомендую).\n"
        "💰 По минимальной цене — самая дешёвая, даже если дальше.\n"
        "📍 По близости — ближайшая с топливом, даже если дороже.\n\n"
        "Какой вариант предпочитаешь?",
        reply_markup=sort_choice_keyboard()
    )

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

@router.callback_query(F.data == "sort_distance")
async def sort_distance(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(sort_mode="distance")
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
                # Создаём пользователя, если его нет (защита)
                user = await create_user(db, message.from_user.id, message.from_user.username)
                logger.info(f"Создан новый пользователь {user.telegram_id} во время поиска")

            # Если у пользователя нет города, пробуем взять из состояния
            if not user.city_id and city_id:
                city = await get_city_by_id(db, city_id)
                if city:
                    user.city_id = city_id
                    await db.commit()
                else:
                    await message.answer("❌ Город не найден. Пожалуйста, выберите город в профиле.")
                    await state.clear()
                    return

            # Если всё равно нет города — просим выбрать
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

            # Получаем город с координатами
            city = await get_city_by_id(db, user.city_id)
            if not city or city.latitude is None or city.longitude is None:
                await message.answer(
                    "❌ У выбранного города не заданы координаты. Обратитесь к администратору.",
                    reply_markup=main_menu_keyboard()
                )
                await state.clear()
                return

            lat = city.latitude
            lon = city.longitude

            # Запоминаем время первого поиска
            await set_first_search(db, user.id)
            is_pro = await check_pro(user.telegram_id)

            # Получаем станции
            stations = await db.execute(
                select(Station).where(Station.city_id == city.id, Station.is_active == True)
            )
            stations = stations.scalars().all()
            if not stations:
                await message.answer("В этом городе пока нет АЗС.", reply_markup=main_menu_keyboard())
                await state.clear()
                return

            station_ids = [s.id for s in stations]

            # Получаем последние цены (свежие)
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

            # Получаем последние отчёты о наличии (свежие)
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

            # Получаем среднюю, минимальную и максимальную цену за 30 дней
            avg_price = await get_avg_price_30d(db, city.id, fuel_type) or 0
            min_price = await get_min_price_30d(db, city.id, fuel_type) or 0
            max_price = await get_max_price_30d(db, city.id, fuel_type) or 0

            # Формируем результаты
            results = []
            for station in stations:
                price_rec = prices.get(station.id)
                if not price_rec:
                    continue
                avail_rec = avails.get(station.id)
                rating_data = calculate_rating(
                    station=station,
                    user_lat=lat,
                    user_lon=lon,
                    price_record=price_rec,
                    availability_record=avail_rec,
                    avg_price_30d=avg_price or price_rec.price,
                    min_price_30d=min_price or price_rec.price,
                    max_price_30d=max_price or price_rec.price
                )
                results.append({
                    "station": station,
                    "price": price_rec.price,
                    "price_time": price_rec.recorded_at,
                    "availability": avail_rec.status if avail_rec else AvailabilityStatus.GRAY,
                    "availability_time": avail_rec.recorded_at if avail_rec else None,
                    "distance_km": haversine_distance(lat, lon, station.latitude, station.longitude),
                    "rating": rating_data["rating"],
                    "explanation": rating_data["explanation"]
                })

            if not results:
                await message.answer("Не найдено АЗС с актуальными ценами. Попробуйте позже.")
                await state.clear()
                return

            # Сортировка
            if sort_mode == "price":
                results.sort(key=lambda x: x["price"])
            elif sort_mode == "distance":
                results.sort(key=lambda x: x["distance_km"])
            else:
                results.sort(key=lambda x: x["rating"], reverse=True)

            await state.update_data(all_results=results, current_index=0, is_pro=is_pro)

            # Показываем лучший результат
            await show_station_card(message, results[0], 0, len(results), is_pro, state)

            # Логируем действие
            await log_action(db, user.id, "search_result")

    except Exception as e:
        logger.error(f"Ошибка в perform_search: {e}", exc_info=True)
        await message.answer("⚠️ Произошла ошибка при поиске. Попробуйте позже.")
        await state.clear()

# ---------- Функция отображения карточки ----------
async def show_station_card(message: types.Message, result: dict, index: int, total: int, is_pro: bool, state: FSMContext):
    try:
        if not result:
            await message.answer("Ошибка: нет данных для отображения.")
            return

        station = result.get("station")
        if not station:
            await message.answer("Ошибка: данные о станции отсутствуют.")
            return

        # Безопасное получение значений с защитой от None
        price = result.get("price", 0.0)
        price_time = result.get("price_time")
        availability = result.get("availability", AvailabilityStatus.GRAY)
        availability_time = result.get("availability_time")
        distance_km = result.get("distance_km", 0.0)
        rating = result.get("rating", 0)
        explanation = result.get("explanation", "")

        status_text = status_emoji(availability.value if availability else "GRAY")
        status_time = format_time_ago(availability_time) if availability_time else "неизвестно"
        price_time_str = format_time_ago(price_time) if price_time else "неизвестно"

        tank_volume = 50
        fuel_consumption = 10
        cost_to_drive = round((distance_km / 100) * fuel_consumption * price, 2) if distance_km and price else 0

        text = (
            f"🏆 Рейтинг: {rating}/100\n"
            f"⛽ {station.name}\n"
            f"📍 {station.address}\n"
            f"💰 {price:.2f} ₽ (обновлено {price_time_str})\n"
            f"{status_text} Наличие: {availability.value if availability else 'GRAY'} ({status_time})\n"
            f"📏 {distance_km:.1f} км (по прямой) | ~{round(distance_km / 40 * 60)} мин\n"
            f"💸 Дорога до АЗС: ~{cost_to_drive} ₽ (из расхода 10 л/100км)\n"
            f"📌 {explanation}\n"
        )

        if not is_pro:
            text += (
                "\n⚠️ <i>Цены и наличие обновляются каждые 2 часа. "
                "Чтобы получать уведомления об изменениях — подключи PRO за 99 ₽/мес.</i>"
            )

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

# ---------- Обработчик "Показать ещё 2 варианта" ----------
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

        text = "📋 Дополнительные варианты:\n\n"
        for i, res in enumerate(more_results, start=next_index+1):
            station = res.get("station")
            if not station:
                continue
            price = res.get("price", 0.0)
            distance = res.get("distance_km", 0.0)
            availability = res.get("availability", AvailabilityStatus.GRAY)
            status = availability.value if availability else "GRAY"

            # Проверка координат перед ссылкой
            if station.latitude and station.longitude:
                map_link = f"<a href='https://yandex.ru/maps/?pt={station.longitude},{station.latitude}&z=15'>Маршрут</a>"
            else:
                map_link = "Координаты отсутствуют"

            text += (
                f"{i}. {station.name}\n"
                f"   Цена: {price:.2f} ₽, наличие: {status}, расстояние: {distance:.1f} км\n"
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

# ---------- Остальные обработчики ----------
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

        # Проверка существующей активной подписки
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

        # Проверка дубликата
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
        "Введите актуальную цену на АИ‑95 на этой АЗС (в рублях, например, 68.50):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_report")]
        ])
    )

@router.message(ReportPriceStates.waiting_price, F.text)
async def process_report_price(message: types.Message, state: FSMContext):
    try:
        price = float(message.text.replace(',', '.'))
        if price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Пожалуйста, введите положительное число (например, 68.50).")
        return
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
        user.reputation += 1
        await db.commit()
    await message.answer(
        f"✅ Спасибо! Цена для {station.name} обновлена до {price:.2f} ₽.\n"
        f"Ваша репутация +1 (всего {user.reputation}).",
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
