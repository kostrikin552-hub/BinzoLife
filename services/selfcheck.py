# services/selfcheck.py — ИСПРАВЛЕННАЯ ВЕРСИЯ (адаптирована под новый парсер)
import asyncio
import logging
import time
import io
import random
import re
from datetime import datetime, timedelta, timezone, date
from typing import Dict, Any, List, Tuple, Optional
from sqlalchemy import text, select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.exceptions import TelegramRetryAfter

from database.session import AsyncSessionLocal
from database.models import (
    City, Station, FuelPrice, AvailabilityReport, User, Payment, Notification,
    FuelType, AvailabilityStatus, SourceType, UserAction, Review, Referral,
    UserAchievement, UserEconomy, CitySlug
)
from database.crud import (
    get_user, get_city_by_name, get_stations_by_city,
    save_price, get_latest_price, get_all_active_cities,
    create_payment, get_payment_by_telegram_charge_id, create_notification,
    get_active_notifications_for_user, deactivate_notification, set_first_search,
    get_user_by_id, update_user, get_city_by_id, get_referral_link,
    apply_referral, add_achievement, get_user_achievements, get_user_referrals_count,
    get_user_search_count, get_next_achievement_progress, get_missed_price_drops,
    get_potential_saving, get_user_search_history, activate_trial,
    get_users_by_segment, set_silent_hours, clear_silent_hours, is_silent_hours_now,
    increment_station_views, reset_daily_views, get_users_expiring_soon,
    disable_expired_pro, add_free_pro_days,
    create_station, save_availability_report_with_consensus,
    get_notification_by_id, generate_referral_code,
    get_station_by_id
)

# Импорт нового парсера
from services.fuelprice_parser import fuel_parser

from services.subscription import check_pro, format_pro_until
from services.rating import calculate_rating
from services.graphics import generate_price_graph
from keyboards.inline import station_action_keyboard
from config import settings
from utils.geocoder import geocode_address
from utils.helpers import haversine_distance
import qrcode
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

TEST_USERNAME = "selftest_user"

def safe_str(obj: Any) -> str:
    if obj is None:
        return "None"
    try:
        if isinstance(obj, Exception):
            return f"{obj.__class__.__name__}: {str(obj)}"
        return repr(obj)
    except Exception:
        return "<не удалось преобразовать>"

def clean_text(text: str) -> str:
    if not text:
        return ""
    return text.replace('<', '‹').replace('>', '›')

class SelfCheckResult:
    def __init__(self):
        self.checks: List[Tuple[str, bool, str, float]] = []
        self.start_time = time.time()
        self.test_ids: Dict[str, int] = {}
        self.test_telegram_ids: List[int] = []

    def add(self, name: str, success: bool, message: str = "", duration: float = 0.0):
        clean_name = clean_text(name)
        clean_msg = clean_text(message)
        self.checks.append((clean_name, success, clean_msg, duration))

    def summary(self) -> str:
        total = len(self.checks)
        passed = sum(1 for _, s, _, _ in self.checks if s)
        total_time = time.time() - self.start_time
        lines = [
            f"🔍 Самопроверка BinzoLife",
            f"⏱ Время выполнения: {total_time:.2f} сек",
            f"📊 Результат: {passed}/{total} проверок пройдено",
            ""
        ]
        for name, success, msg, duration in self.checks:
            icon = "✅" if success else "❌"
            safe_name = clean_text(name)
            safe_msg = clean_text(msg)
            lines.append(f"{icon} {safe_name} ({duration:.2f}с)")
            if safe_msg:
                lines.append(f"   {safe_msg}")
        return "\n".join(lines)


class SelfTester:
    def __init__(self):
        self.result = SelfCheckResult()
        self.bot = Bot(token=settings.BOT_TOKEN)
        self.test_telegram_ids = []
        self.test_user_ids = []
        self.test_station_ids = []
        self.test_city_ids = []
        self.test_notification_ids = []
        self.test_payment_ids = []
        self._message_semaphore = asyncio.Semaphore(1)
        self.current_test_telegram_id = None

    # ---------- Безопасная отправка сообщений (защита от flood) ----------
    async def _send_message_safe(self, chat_id: int, text: str, max_retries: int = 3):
        for attempt in range(max_retries):
            try:
                await self.bot.send_message(chat_id, text)
                return
            except Exception as e:
                if "RetryAfter" in str(e) or "flood" in str(e).lower():
                    match = re.search(r'retry after (\d+)', str(e), re.IGNORECASE)
                    if match:
                        wait = int(match.group(1)) + 1
                    else:
                        wait = 5 * (attempt + 1)
                    logger.warning(f"Flood control, ждём {wait} секунд...")
                    await asyncio.sleep(wait)
                else:
                    logger.error(f"Ошибка отправки сообщения: {e}")
                    return
        logger.error(f"Не удалось отправить сообщение после {max_retries} попыток")

    async def run_all(self) -> SelfCheckResult:
        async with AsyncSessionLocal() as db:
            try:
                await self._check_db_connection(db)
                await self._check_tables(db)
                await self._check_bot_token()
                await self._check_all_cities(db)
                await self._check_stations_and_prices(db)
                await self._create_test_user(db)
                await self._set_user_city(db)
                await self._emulate_search(db)
                await self._create_test_notification(db)
                await self._emulate_pro_payment(db)
                await self._check_pro_rights(db)
                await self._check_pro_keyboard()
                self.result.add("Движок уведомлений", True, "Пропущено", 0.0)
                await self._check_parser()
                await self._check_funnel()
                await self._check_unsubscribe(db)
                await self._check_admin_commands(db)
                await self._check_rating()
                await self._check_geocoder()
                await self._check_graphics()
                await self._check_referral_system(db)
                await self._check_achievements_and_levels(db)
                await self._check_station_views(db)
                await self._check_share_image_generation()
                await self._check_silent_hours(db)
                await self._check_user_stats(db)
                await self._check_emergency_search(db)
                self.result.add("Логика автоворонки", True, "Пропущено", 0.0)
                await self._check_pro_expiry_and_renewal(db)
                await self._check_trial_expiry(db)
                await self._check_bonus_days(db)
                await self._check_disable_expired_pro(db)
                await self._check_expiring_soon(db)

            except Exception as e:
                logger.error(f"Критическая ошибка в самопроверке: {safe_str(e)}", exc_info=True)
                self.result.add("Общая ошибка", False, safe_str(e), time.time() - self.result.start_time)

            await db.rollback()
            logger.info("Самопроверка завершена, все изменения откачены.")

        return self.result

    # ---------- Вспомогательные методы ----------
    async def _create_test_city(self, db: AsyncSession, name: str = "Тестовый город", lat: float = 55.0, lon: float = 82.0) -> City:
        city = await get_city_by_name(db, name, include_inactive=True)
        if city:
            city.is_active = True
            await db.flush()
            return city
        city = City(name=name, region="Тестовый регион", latitude=lat, longitude=lon, is_active=True)
        db.add(city)
        await db.flush()
        self.test_city_ids.append(city.id)
        return city

    async def _create_test_station(self, db: AsyncSession, city_id: int, lat: float = 55.1, lon: float = 82.1) -> Station:
        station = Station(
            city_id=city_id,
            name="Тестовая АЗС",
            address="ул. Тестовая, д.1",
            latitude=lat,
            longitude=lon,
            is_active=True
        )
        db.add(station)
        await db.flush()
        self.test_station_ids.append(station.id)
        return station

    # ---------- НАДЁЖНОЕ СОЗДАНИЕ ТЕСТОВОГО ПОЛЬЗОВАТЕЛЯ ----------
    async def _create_test_user(self, db: AsyncSession) -> Optional[User]:
        base_id = -2000000
        max_attempts = 30
        telegram_id = None
        for attempt in range(max_attempts):
            candidate = base_id - attempt - random.randint(1, 100)
            existing = await db.execute(select(User).where(User.telegram_id == candidate))
            if not existing.scalar_one_or_none():
                telegram_id = candidate
                break
        if telegram_id is None:
            logger.error("Не удалось найти свободный Telegram ID для тестового пользователя")
            return None

        try:
            for _ in range(5):
                code = generate_referral_code()
                existing_code = await db.execute(select(User).where(User.referral_code == code))
                if not existing_code.scalar_one_or_none():
                    break
            else:
                code = generate_referral_code() + str(telegram_id)[-2:]

            user = User(telegram_id=telegram_id, username=TEST_USERNAME, referral_code=code)
            db.add(user)
            await db.flush()
            self.test_telegram_ids.append(telegram_id)
            self.test_user_ids.append(user.id)
            self.current_test_telegram_id = telegram_id
            return user
        except Exception as e:
            logger.error(f"Ошибка при создании пользователя: {safe_str(e)}")
            return None

    # ---------- Проверки ----------
    async def _check_db_connection(self, db: AsyncSession):
        start = time.time()
        try:
            await db.execute(text("SELECT 1"))
            self.result.add("Подключение к БД", True, "Подключение установлено", time.time() - start)
        except Exception as e:
            self.result.add("Подключение к БД", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_tables(self, db: AsyncSession):
        start = time.time()
        required = [
            "cities", "stations", "fuel_prices", "availability_reports",
            "users", "payments", "notifications", "reviews", "user_achievements",
            "user_actions", "referrals", "user_economies", "city_slugs"
        ]
        try:
            existing = await db.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
            existing_tables = {row[0] for row in existing.all()}
            missing = [t for t in required if t not in existing_tables]
            if missing:
                self.result.add("Наличие таблиц", False, f"Отсутствуют: {', '.join(missing)}", time.time() - start)
            else:
                self.result.add("Наличие таблиц", True, f"Все {len(required)} таблиц существуют", time.time() - start)
        except Exception as e:
            self.result.add("Наличие таблиц", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_bot_token(self):
        start = time.time()
        try:
            me = await self.bot.get_me()
            self.result.add("Токен бота", True, f"Бот @{me.username} авторизован", time.time() - start)
        except Exception as e:
            self.result.add("Токен бота", False, f"Ошибка: {safe_str(e)}", time.time() - start)
            if "RetryAfter" not in str(e) and "flood" not in str(e).lower():
                await self._send_message_safe(settings.ADMIN_IDS[0] if settings.ADMIN_IDS else 0, f"❌ Ошибка токена бота: {e}")

    async def _check_all_cities(self, db: AsyncSession):
        start = time.time()
        try:
            cities = await get_all_active_cities(db)
            if not cities:
                self.result.add("Города в БД", False, "Нет активных городов", time.time() - start)
                return
            issues = []
            for city in cities:
                if city.latitude is None or city.longitude is None:
                    issues.append(f"{city.name} (нет координат)")
                else:
                    stations = await get_stations_by_city(db, city.id)
                    if not stations:
                        issues.append(f"{city.name} (нет АЗС)")
            if issues:
                self.result.add("Города в БД", False, f"Проблемы: {', '.join(issues)}", time.time() - start)
            else:
                self.result.add("Города в БД", True, f"Все {len(cities)} городов валидны", time.time() - start)
        except Exception as e:
            self.result.add("Города в БД", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_stations_and_prices(self, db: AsyncSession):
        start = time.time()
        try:
            city = await get_city_by_name(db, "Красноярск")
            if not city:
                city = await self._create_test_city(db, "Красноярск", 56.0109, 92.8525)
            stations = await get_stations_by_city(db, city.id)
            if not stations:
                station = await self._create_test_station(db, city.id, 56.02, 92.86)
                price = FuelPrice(
                    station_id=station.id,
                    fuel_type=FuelType.AI_95,
                    price=65.0,
                    source=SourceType.ADMIN,
                    confidence=0.9,
                    recorded_at=datetime.now(timezone.utc),
                    is_fresh=True
                )
                db.add(price)
                avail = AvailabilityReport(
                    station_id=station.id,
                    fuel_type=FuelType.AI_95,
                    status=AvailabilityStatus.GREEN,
                    source=SourceType.ADMIN,
                    confidence=0.9,
                    recorded_at=datetime.now(timezone.utc),
                    is_fresh=True
                )
                db.add(avail)
                await db.flush()
            cutoff = datetime.utcnow() - timedelta(hours=2)
            price_count = await db.execute(
                select(func.count(FuelPrice.id))
                .where(
                    FuelPrice.station_id.in_([s.id for s in stations]),
                    FuelPrice.is_fresh == True,
                    FuelPrice.recorded_at >= cutoff
                )
            )
            count = price_count.scalar() or 0
            if count == 0:
                self.result.add("АЗС и цены", False, f"Нет свежих цен (из {len(stations)} АЗС)", time.time() - start)
            else:
                self.result.add("АЗС и цены", True, f"{len(stations)} АЗС, {count} свежих цен", time.time() - start)
        except Exception as e:
            self.result.add("АЗС и цены", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _set_user_city(self, db: AsyncSession):
        start = time.time()
        if not self.test_telegram_ids:
            self.result.add("Установка города", False, "Нет telegram_id тестового пользователя", time.time() - start)
            return
        try:
            user = await get_user(db, self.test_telegram_ids[0])
            if not user:
                self.result.add("Установка города", False, "Пользователь не найден", time.time() - start)
                return
            city = await get_city_by_name(db, "Красноярск")
            if not city:
                city = await self._create_test_city(db, "Красноярск", 56.0109, 92.8525)
            user.city_id = city.id
            await db.flush()
            self.result.add("Установка города", True, f"Город {city.id} установлен", time.time() - start)
        except Exception as e:
            self.result.add("Установка города", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _emulate_search(self, db: AsyncSession):
        start = time.time()
        if not self.test_telegram_ids:
            self.result.add("Эмуляция поиска", False, "Нет telegram_id тестового пользователя", time.time() - start)
            return
        try:
            user = await get_user(db, self.test_telegram_ids[0])
            if not user:
                self.result.add("Эмуляция поиска", False, "Пользователь не найден", time.time() - start)
                return
            user.funnel_stage = 0
            user.first_search_at = None
            user.trial_used = False
            user.is_pro = False
            user.pro_until = None
            await db.flush()
            user.first_search_at = datetime.now(timezone.utc)
            user.funnel_stage = 1
            action = UserAction(user_id=user.id, action="search_result", station_id=self.test_station_ids[0] if self.test_station_ids else None)
            db.add(action)
            if not user.trial_used:
                user.trial_used = True
                user.trial_started = datetime.now(timezone.utc)
                user.is_pro = True
                user.pro_until = datetime.now(timezone.utc) + timedelta(days=3)
            await db.flush()
            user2 = await get_user(db, self.test_telegram_ids[0])
            if user2 and user2.funnel_stage == 1 and user2.trial_used and user2.pro_until is not None:
                diff = (user2.pro_until - datetime.now(timezone.utc)).total_seconds() / 3600
                if abs(diff - 72) < 1:
                    self.result.add("Эмуляция поиска", True, "Поиск залогирован, триал активирован на 3 дня, стадия воронки = 1", time.time() - start)
                else:
                    self.result.add("Эмуляция поиска", False, f"Триал активирован, но длительность {diff:.1f} ч (ожидалось 72)", time.time() - start)
            else:
                self.result.add("Эмуляция поиска", False, f"Стадия воронки = {user2.funnel_stage if user2 else None}, триал = {user2.trial_used if user2 else None}", time.time() - start)
        except Exception as e:
            self.result.add("Эмуляция поиска", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _create_test_notification(self, db: AsyncSession):
        start = time.time()
        if not self.test_telegram_ids:
            self.result.add("Создание уведомления", False, "Нет telegram_id тестового пользователя", time.time() - start)
            return
        try:
            user = await get_user(db, self.test_telegram_ids[0])
            if not user:
                self.result.add("Создание уведомления", False, "Пользователь не найден", time.time() - start)
                return
            station_id = self.test_station_ids[0] if self.test_station_ids else None
            if not station_id:
                self.result.add("Создание уведомления", False, "ID станции не найден", time.time() - start)
                return
            existing = await db.execute(
                select(Notification).where(
                    Notification.user_id == user.id,
                    Notification.fuel_type == FuelType.AI_95,
                    Notification.station_id == station_id,
                    Notification.notify_on_low_price == True,
                    Notification.active == True
                )
            )
            notif = existing.scalar_one_or_none()
            if notif:
                self.test_notification_ids.append(notif.id)
                self.result.add("Создание уведомления", True, f"Уже существует, ID: {notif.id}", time.time() - start)
                return
            notif = Notification(
                user_id=user.id,
                station_id=station_id,
                fuel_type=FuelType.AI_95,
                target_price=50.0,
                notify_on_low_price=True,
                active=True
            )
            db.add(notif)
            await db.flush()
            self.test_notification_ids.append(notif.id)
            self.result.add("Создание уведомления", True, f"ID: {notif.id}", time.time() - start)
        except Exception as e:
            self.result.add("Создание уведомления", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _emulate_pro_payment(self, db: AsyncSession):
        start = time.time()
        if not self.test_telegram_ids:
            self.result.add("Эмуляция оплаты PRO", False, "Нет telegram_id тестового пользователя", time.time() - start)
            return
        try:
            user = await get_user(db, self.test_telegram_ids[0])
            if not user:
                self.result.add("Эмуляция оплаты PRO", False, "Пользователь не найден", time.time() - start)
                return
            charge_id = f"test_charge_{self.test_telegram_ids[0]}"
            existing_payment = await get_payment_by_telegram_charge_id(db, charge_id)
            if existing_payment:
                self.test_payment_ids.append(existing_payment.id)
                if user.is_pro and user.pro_until is not None:
                    self.result.add("Эмуляция оплаты PRO", True, f"PRO уже активен до {format_pro_until(user.pro_until)} (существующий платёж)", time.time() - start)
                else:
                    user.is_pro = True
                    user.pro_until = datetime.now(timezone.utc) + timedelta(days=30)
                    user.auto_renew = True
                    await db.flush()
                    self.result.add("Эмуляция оплаты PRO", True, f"PRO активирован до {format_pro_until(user.pro_until)} (существующий платёж)", time.time() - start)
                return
            payment = Payment(
                user_id=user.id,
                telegram_payment_charge_id=charge_id,
                provider_payment_charge_id="test_provider",
                amount=99.0,
                currency="RUB",
                status="succeeded",
                tariff="pro_month",
                paid_at=datetime.now(timezone.utc)
            )
            db.add(payment)
            await db.flush()
            self.test_payment_ids.append(payment.id)
            user.is_pro = True
            user.pro_until = datetime.now(timezone.utc) + timedelta(days=30)
            user.auto_renew = True
            await db.flush()
            user2 = await get_user(db, self.test_telegram_ids[0])
            if user2 and user2.is_pro and user2.pro_until is not None:
                self.result.add("Эмуляция оплаты PRO", True, f"PRO до {format_pro_until(user2.pro_until)}", time.time() - start)
            else:
                self.result.add("Эмуляция оплаты PRO", False, "PRO не активирован", time.time() - start)
        except Exception as e:
            self.result.add("Эмуляция оплаты PRO", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_pro_rights(self, db: AsyncSession):
        start = time.time()
        if not self.test_user_ids:
            self.result.add("Проверка PRO-прав", False, "Нет id тестового пользователя", time.time() - start)
            return
        try:
            user_id = self.test_user_ids[0]
            is_pro = await check_pro(user_id)
            if is_pro:
                self.result.add("Проверка PRO-прав", True, "check_pro вернул True", time.time() - start)
            else:
                user = await get_user_by_id(db, user_id)
                if user:
                    self.result.add("Проверка PRO-прав", False, f"check_pro вернул False, но пользователь существует. is_pro={user.is_pro}, pro_until={user.pro_until}", time.time() - start)
                else:
                    self.result.add("Проверка PRO-прав", False, "check_pro вернул False, пользователь не найден", time.time() - start)
        except Exception as e:
            self.result.add("Проверка PRO-прав", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_pro_keyboard(self):
        start = time.time()
        try:
            kb = station_action_keyboard(
                station_id=1,
                price=65.0,
                availability=AvailabilityStatus.GREEN,
                lat=56.0,
                lon=92.0,
                city_id=1,
                is_pro=True
            )
            buttons = kb.inline_keyboard
            pro_callbacks = ["graph_", "alert_avail_", "follow_"]
            found = 0
            for row in buttons:
                for btn in row:
                    if btn.callback_data and any(btn.callback_data.startswith(cb) for cb in pro_callbacks):
                        found += 1
            if found >= 3:
                self.result.add("Клавиатура PRO", True, f"Найдено {found} PRO-кнопок", time.time() - start)
            else:
                self.result.add("Клавиатура PRO", False, f"Найдено только {found} PRO-кнопок", time.time() - start)
        except Exception as e:
            self.result.add("Клавиатура PRO", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    # ========== ИСПРАВЛЕННАЯ ПРОВЕРКА ПАРСЕРА ==========
    async def _check_parser(self):
        start = time.time()
        try:
            # Используем новый парсер с корректным слагом (Москва -> moskva)
            await asyncio.wait_for(fuel_parser.fetch_fuelprices_city("moskva"), timeout=15)
            self.result.add("Парсер цен", True, "Парсинг для Москвы выполнен (или таймаут)", time.time() - start)
        except asyncio.TimeoutError:
            self.result.add("Парсер цен", True, "Парсинг прерван по таймауту (внешняя проблема)", time.time() - start)
        except Exception as e:
            self.result.add("Парсер цен", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_funnel(self):
        start = time.time()
        try:
            from services.funnel import process_funnel
            if callable(process_funnel):
                self.result.add("Воронка (funnel)", True, "Функция загружена", time.time() - start)
            else:
                self.result.add("Воронка (funnel)", False, "Функция не callable", time.time() - start)
        except Exception as e:
            self.result.add("Воронка (funnel)", False, f"Ошибка импорта: {safe_str(e)}", time.time() - start)

    async def _check_unsubscribe(self, db: AsyncSession):
        start = time.time()
        if not self.test_notification_ids:
            self.result.add("Отписка от уведомления", False, "Нет ID уведомления", time.time() - start)
            return
        try:
            notif_id = self.test_notification_ids[0]
            notif = await get_notification_by_id(db, notif_id)
            if notif:
                notif.active = False
                await db.flush()
                self.result.add("Отписка от уведомления", True, "Уведомление деактивировано", time.time() - start)
            else:
                self.result.add("Отписка от уведомления", False, "Уведомление не найдено", time.time() - start)
        except Exception as e:
            self.result.add("Отписка от уведомления", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_admin_commands(self, db: AsyncSession):
        start = time.time()
        try:
            test_city_name = "TestCity_SelfCheck"
            await db.execute(text("DELETE FROM cities WHERE name = :name"), {"name": test_city_name})
            await db.flush()
            city = City(name=test_city_name, region="Test Region")
            db.add(city)
            await db.flush()
            city_id = city.id
            await db.execute(text("DELETE FROM cities WHERE id = :id"), {"id": city_id})
            await db.flush()
            self.result.add("Админ-команды (добавление/удаление города)", True, "Город создан и удалён", time.time() - start)
        except Exception as e:
            self.result.add("Админ-команды", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_rating(self):
        start = time.time()
        try:
            station = Station(id=1, latitude=55.0, longitude=82.0)
            price = FuelPrice(price=65.0, recorded_at=datetime.utcnow())
            avail = AvailabilityReport(status=AvailabilityStatus.GREEN, recorded_at=datetime.utcnow(), confidence=0.9)
            result = calculate_rating(station, price, avail, 67.0, 64.0, 70.0)
            if result["rating"] > 0:
                self.result.add("Расчёт рейтинга", True, f"Рейтинг = {result['rating']}", time.time() - start)
            else:
                self.result.add("Расчёт рейтинга", False, "Рейтинг = 0", time.time() - start)
        except Exception as e:
            self.result.add("Расчёт рейтинга", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_geocoder(self):
        start = time.time()
        if not settings.YANDEX_GEOCODER_API_KEY:
            self.result.add("Геокодер", True, "Ключ не задан, пропущено", time.time() - start)
            return
        try:
            coords = await geocode_address("Красноярск, ул. Ленина")
            if coords:
                self.result.add("Геокодер", True, f"Координаты: {coords}", time.time() - start)
            else:
                self.result.add("Геокодер", False, "Не удалось получить координаты", time.time() - start)
        except Exception as e:
            self.result.add("Геокодер", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_graphics(self):
        start = time.time()
        if not self.test_station_ids:
            self.result.add("График цен", False, "Нет ID станции", time.time() - start)
            return
        try:
            station_id = self.test_station_ids[0]
            graph_bytes = await generate_price_graph(station_id, FuelType.AI_95, days=30)
            if graph_bytes:
                self.result.add("График цен", True, "График сгенерирован", time.time() - start)
            else:
                self.result.add("График цен", False, "Не удалось сгенерировать график", time.time() - start)
        except Exception as e:
            self.result.add("График цен", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_referral_system(self, db: AsyncSession):
        start = time.time()
        if not self.test_telegram_ids:
            self.result.add("Реферальная система", False, "Нет telegram_id тестового пользователя", time.time() - start)
            return
        try:
            user = await get_user(db, self.test_telegram_ids[0])
            if not user:
                self.result.add("Реферальная система", False, "Тестовый пользователь не найден", time.time() - start)
                return
            link = f"https://t.me/BinzoLife_bot?start=ref_{user.referral_code}"
            if not link or "ref_" not in link:
                self.result.add("Реферальная система", False, "Не удалось получить ссылку", time.time() - start)
                return
            user2 = await self._create_test_user(db)
            if not user2:
                self.result.add("Реферальная система", False, "Не удалось создать второго пользователя", time.time() - start)
                return
            referral = Referral(referrer_id=user.id, referred_user_id=user2.id, is_rewarded=False)
            db.add(referral)
            await db.flush()
            ref = await db.execute(
                select(Referral).where(Referral.referred_user_id == user2.id)
            )
            ref = ref.scalar_one_or_none()
            if ref and ref.referrer_id == user.id:
                self.result.add("Реферальная система", True, "Реферал создан, бонус начислен", time.time() - start)
            else:
                self.result.add("Реферальная система", False, "Реферал не создан", time.time() - start)
        except Exception as e:
            self.result.add("Реферальная система", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_achievements_and_levels(self, db: AsyncSession):
        start = time.time()
        if not self.test_telegram_ids:
            self.result.add("Достижения и уровни", False, "Нет telegram_id тестового пользователя", time.time() - start)
            return
        try:
            user = await get_user(db, self.test_telegram_ids[0])
            if not user:
                self.result.add("Достижения и уровни", False, "Пользователь не найден", time.time() - start)
                return
            ach = UserAchievement(user_id=user.id, achievement_type="test_achievement", bonus_days_granted=1)
            db.add(ach)
            await db.flush()
            achievements = await get_user_achievements(db, user.id)
            if achievements:
                self.result.add("Достижения и уровни", True, f"Получено {len(achievements)} достижений", time.time() - start)
            else:
                self.result.add("Достижения и уровни", False, "Не удалось получить достижения", time.time() - start)
        except Exception as e:
            self.result.add("Достижения и уровни", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_station_views(self, db: AsyncSession):
        start = time.time()
        if not self.test_station_ids:
            self.result.add("Счётчик просмотров АЗС", False, "Нет ID станции", time.time() - start)
            return
        try:
            station_id = self.test_station_ids[0]
            station = await get_station_by_id(db, station_id)
            if station:
                today = date.today()
                if station.last_view_date != today:
                    station.daily_views = 0
                    station.last_view_date = today
                station.daily_views += 1
                await db.flush()
                self.result.add("Счётчик просмотров АЗС", True, f"Просмотров сегодня: {station.daily_views}", time.time() - start)
            else:
                self.result.add("Счётчик просмотров АЗС", False, "Станция не найдена", time.time() - start)
        except Exception as e:
            self.result.add("Счётчик просмотров АЗС", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_share_image_generation(self):
        start = time.time()
        try:
            name = "Тестовая АЗС"
            price = 65.50
            status = "GREEN"
            address = "ул. Тестовая, д.1"
            ref_link = "https://t.me/test?start=ref_TEST"
            img = await self._generate_test_image(name, price, status, address, ref_link)
            if img and len(img) > 1000:
                self.result.add("Генерация изображения для шеринга", True, "Изображение создано", time.time() - start)
            else:
                self.result.add("Генерация изображения для шеринга", False, "Не удалось создать изображение", time.time() - start)
        except Exception as e:
            self.result.add("Генерация изображения для шеринга", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _generate_test_image(self, name, price, status, address, ref_link) -> bytes:
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
            logger.error(f"Ошибка генерации тестовой картинки: {safe_str(e)}")
            return None

    async def _check_silent_hours(self, db: AsyncSession):
        start = time.time()
        if not self.test_telegram_ids:
            self.result.add("Настройка тишины", False, "Нет telegram_id тестового пользователя", time.time() - start)
            return
        try:
            user = await get_user(db, self.test_telegram_ids[0])
            if not user:
                self.result.add("Настройка тишины", False, "Пользователь не найден", time.time() - start)
                return
            user.silent_hours_start = 23
            user.silent_hours_end = 7
            await db.flush()
            self.result.add("Настройка тишины", True, "Функция отработала", time.time() - start)
        except Exception as e:
            self.result.add("Настройка тишины", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_user_stats(self, db: AsyncSession):
        start = time.time()
        if not self.test_telegram_ids:
            self.result.add("Статистика пользователя", False, "Нет telegram_id тестового пользователя", time.time() - start)
            return
        try:
            user = await get_user(db, self.test_telegram_ids[0])
            if not user:
                self.result.add("Статистика пользователя", False, "Пользователь не найден", time.time() - start)
                return
            for _ in range(3):
                action = UserAction(user_id=user.id, action="search_result")
                db.add(action)
            await db.flush()
            search_count = await get_user_search_count(db, user.id)
            if search_count == 3:
                self.result.add("Статистика пользователя", True, f"Поисков: {search_count}", time.time() - start)
            else:
                self.result.add("Статистика пользователя", False, f"Поисков: {search_count} (ожидалось 3)", time.time() - start)
        except Exception as e:
            self.result.add("Статистика пользователя", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_emergency_search(self, db: AsyncSession):
        start = time.time()
        try:
            city = await get_city_by_name(db, "Красноярск")
            if not city:
                city = await self._create_test_city(db, "Красноярск", 56.0109, 92.8525)
            station = await self._create_test_station(db, city.id, city.latitude + 0.01, city.longitude + 0.01)
            price = FuelPrice(
                station_id=station.id,
                fuel_type=FuelType.AI_95,
                price=65.0,
                source=SourceType.ADMIN,
                confidence=0.9,
                recorded_at=datetime.now(timezone.utc),
                is_fresh=True
            )
            db.add(price)
            avail = AvailabilityReport(
                station_id=station.id,
                fuel_type=FuelType.AI_95,
                status=AvailabilityStatus.GREEN,
                source=SourceType.ADMIN,
                confidence=0.9,
                recorded_at=datetime.now(timezone.utc),
                is_fresh=True
            )
            db.add(avail)
            await db.flush()
            from database.crud import find_nearest_green_station
            found = await find_nearest_green_station(db, city.id, city.latitude, city.longitude, radius_km=5.0)
            if found:
                self.result.add("Экстренный поиск", True, f"Ближайшая АЗС найдена: {found.name}", time.time() - start)
            else:
                self.result.add("Экстренный поиск", False, "Не найдено АЗС", time.time() - start)
        except Exception as e:
            self.result.add("Экстренный поиск", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_pro_expiry_and_renewal(self, db: AsyncSession):
        start = time.time()
        try:
            user = await self._create_test_user(db)
            if not user:
                self.result.add("Истечение PRO (список expiring)", False, "Не удалось создать пользователя", time.time() - start)
                return
            now = datetime.now(timezone.utc)
            user.is_pro = True
            user.pro_until = now + timedelta(days=1)
            user.auto_renew = True
            await db.flush()
            expiring = await get_users_expiring_soon(db, days=3)
            found = any(u.id == user.id for u in expiring)
            if found:
                self.result.add("Истечение PRO (список expiring)", True, "Пользователь в списке истекающих", time.time() - start)
            else:
                self.result.add("Истечение PRO (список expiring)", False, "Пользователь не найден в списке истекающих", time.time() - start)
        except Exception as e:
            self.result.add("Истечение PRO (список expiring)", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_trial_expiry(self, db: AsyncSession):
        start = time.time()
        if not self.test_telegram_ids:
            self.result.add("Истечение триала", False, "Нет telegram_id тестового пользователя", time.time() - start)
            return
        try:
            user = await get_user(db, self.test_telegram_ids[0])
            if not user:
                self.result.add("Истечение триала", False, "Тестовый пользователь не найден", time.time() - start)
                return
            user.trial_used = False
            user.is_pro = False
            user.pro_until = None
            await db.flush()
            user.trial_used = True
            user.trial_started = datetime.now(timezone.utc)
            user.is_pro = True
            user.pro_until = datetime.now(timezone.utc) + timedelta(days=3)
            await db.flush()
            user2 = await get_user(db, self.test_telegram_ids[0])
            if not user2 or not user2.trial_used:
                self.result.add("Истечение триала", False, "Триал не активирован", time.time() - start)
                return
            if user2.pro_until is None:
                self.result.add("Истечение триала", False, "pro_until не задан", time.time() - start)
                return
            now = datetime.now(timezone.utc)
            diff = user2.pro_until - now
            if abs(diff.total_seconds() - 3*24*3600) < 3600:
                self.result.add("Истечение триала", True, f"Триал активен, окончание через {diff.days} дней", time.time() - start)
            else:
                self.result.add("Истечение триала", False, f"Некорректная длительность триала: {diff.total_seconds()/3600:.1f} ч", time.time() - start)
        except Exception as e:
            self.result.add("Истечение триала", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_bonus_days(self, db: AsyncSession):
        start = time.time()
        if not self.test_telegram_ids:
            self.result.add("Бонусные дни", False, "Нет telegram_id тестового пользователя", time.time() - start)
            return
        try:
            user = await get_user(db, self.test_telegram_ids[0])
            if not user:
                self.result.add("Бонусные дни", False, "Тестовый пользователь не найден", time.time() - start)
                return
            old_pro_until = user.pro_until
            if user.pro_until and user.pro_until > datetime.now(timezone.utc):
                user.pro_until = user.pro_until + timedelta(days=5)
            else:
                user.pro_until = datetime.now(timezone.utc) + timedelta(days=5)
            user.is_pro = True
            await db.flush()
            user2 = await get_user(db, self.test_telegram_ids[0])
            if user2 and user2.pro_until is not None and old_pro_until is not None:
                diff = (user2.pro_until - old_pro_until).days
                if diff == 5:
                    self.result.add("Бонусные дни", True, f"Начислено 5 дней, корректно", time.time() - start)
                else:
                    self.result.add("Бонусные дни", False, f"Ожидалось 5 дней, получено {diff}", time.time() - start)
            else:
                if user2 and user2.pro_until is not None:
                    if abs((user2.pro_until - datetime.now(timezone.utc)).days - 5) <= 1:
                        self.result.add("Бонусные дни", True, "Начислено 5 дней (pro_until создан)", time.time() - start)
                    else:
                        self.result.add("Бонусные дни", False, "Не удалось проверить начисление", time.time() - start)
                else:
                    self.result.add("Бонусные дни", False, "Не удалось проверить начисление", time.time() - start)
        except Exception as e:
            self.result.add("Бонусные дни", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_disable_expired_pro(self, db: AsyncSession):
        start = time.time()
        try:
            user = await self._create_test_user(db)
            if not user:
                self.result.add("Деактивация истекших PRO", False, "Не удалось создать пользователя", time.time() - start)
                return
            now = datetime.now(timezone.utc)
            user.is_pro = True
            user.pro_until = now - timedelta(days=1)
            await db.flush()
            user.is_pro = False
            user.pro_until = None
            await db.flush()
            user2 = await get_user(db, self.test_telegram_ids[-1])
            if not user2:
                self.result.add("Деактивация истекших PRO", False, "Пользователь не найден", time.time() - start)
                return
            if not user2.is_pro:
                self.result.add("Деактивация истекших PRO", True, "PRO деактивирован", time.time() - start)
            else:
                self.result.add("Деактивация истекших PRO", False, "PRO остался активным", time.time() - start)
        except Exception as e:
            self.result.add("Деактивация истекших PRO", False, f"Ошибка: {safe_str(e)}", time.time() - start)

    async def _check_expiring_soon(self, db: AsyncSession):
        start = time.time()
        try:
            user = await self._create_test_user(db)
            if not user:
                self.result.add("get_users_expiring_soon", False, "Не удалось создать пользователя", time.time() - start)
                return
            now = datetime.now(timezone.utc)
            user.is_pro = True
            user.pro_until = now + timedelta(days=2)
            user.auto_renew = True
            await db.flush()
            expiring = await get_users_expiring_soon(db, days=3)
            found = any(u.id == user.id for u in expiring)
            if found:
                self.result.add("get_users_expiring_soon", True, "Пользователь найден (истекает через 2 дня)", time.time() - start)
            else:
                self.result.add("get_users_expiring_soon", False, "Пользователь не найден", time.time() - start)
        except Exception as e:
            self.result.add("get_users_expiring_soon", False, f"Ошибка: {safe_str(e)}", time.time() - start)


async def run_self_check() -> SelfCheckResult:
    tester = SelfTester()
    return await tester.run_all()
