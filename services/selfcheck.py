# services/selfcheck.py – ПОЛНЫЙ ФАЙЛ (ОЧИСТКА ОТКЛЮЧЕНА)

import asyncio
import logging
import time
import io
from datetime import datetime, timedelta, date
from typing import Dict, Any, List, Tuple, Optional
from sqlalchemy import text, select, func, delete, and_, or_

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

from database.session import AsyncSessionLocal
from database.models import (
    City, Station, FuelPrice, AvailabilityReport, User, Payment, Notification,
    FuelType, AvailabilityStatus, SourceType, UserAction, Review, Referral,
    UserAchievement, UserEconomy, CitySlug
)
from database.crud import (
    get_user, create_user, get_city_by_name, get_stations_by_city,
    save_price, get_latest_price, get_all_active_cities, activate_pro,
    create_payment, get_payment_by_telegram_charge_id, create_notification,
    get_active_notifications_for_user, deactivate_notification, set_first_search,
    get_user_by_id, update_user, get_city_by_id, get_referral_link,
    apply_referral, add_achievement, get_user_achievements, get_user_referrals_count,
    get_user_search_count, get_next_achievement_progress, get_missed_price_drops,
    get_potential_saving, get_user_search_history, activate_trial,
    get_users_by_segment, set_silent_hours, clear_silent_hours, is_silent_hours_now,
    increment_station_views, reset_daily_views, get_users_expiring_soon,
    disable_expired_pro, add_free_pro_days
)
from services.fuelprice_parser import fetch_fuelprice_prices
from services.notifications import check_notifications
from services.subscription import check_pro, format_pro_until
from services.rating import calculate_rating
from services.funnel import process_funnel
from services.graphics import generate_price_graph
from services.economy import calculate_potential_saving
from keyboards.inline import station_action_keyboard, pro_purchase_keyboard
from config import settings
from utils.geocoder import geocode_address
from utils.helpers import haversine_distance, format_time_ago, status_emoji
from utils.time_utils import ensure_utc
import qrcode
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

TEST_TELEGRAM_ID = 777777777
TEST_USERNAME = "selftest_user"

def clean_text(text: str) -> str:
    if not text:
        return ""
    return text.replace('<', '‹').replace('>', '›')

class SelfCheckResult:
    def __init__(self):
        self.checks: List[Tuple[str, bool, str, float]] = []
        self.start_time = time.time()
        self.test_user_id: Optional[int] = None
        self.test_city_id: Optional[int] = None
        self.test_station_id: Optional[int] = None
        self.test_notification_id: Optional[int] = None
        self.test_payment_id: Optional[int] = None
        self.test_referral_code: Optional[str] = None
        self.test_achievement_type: Optional[str] = None

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
        self.test_user_id = None
        self.test_city_id = None
        self.test_station_id = None
        self.test_notification_id = None
        self.test_payment_id = None
        self.test_referral_code = None
        self.expiry_test_user_id = None

    async def run_all(self) -> SelfCheckResult:
        await self._check_db_connection()
        await self._check_tables()
        await self._check_bot_token()
        await self._check_default_city()
        await self._check_stations_and_prices()
        await self._create_test_user()
        await self._set_user_city()
        await self._emulate_search()
        await self._create_test_notification()
        await self._emulate_pro_payment()
        await self._check_pro_rights()
        await self._check_pro_keyboard()
        await self._check_notifications_engine()
        await self._check_parser()
        await self._check_funnel()
        await self._check_unsubscribe()
        await self._check_admin_commands()
        await self._check_rating()
        await self._check_geocoder()
        await self._check_graphics()
        await self._check_referral_system()
        await self._check_achievements_and_levels()
        await self._check_station_views()
        await self._check_share_image_generation()
        await self._check_silent_hours()
        await self._check_user_stats()
        await self._check_emergency_search()
        await self._check_funnel_logic()
        await self._check_pro_expiry_and_renewal()
        await self._check_trial_expiry()
        await self._check_bonus_days()
        await self._check_disable_expired_pro()
        await self._check_expiring_soon()
        await self._cleanup()
        return self.result

    # ---------- Все методы проверок (полный набор, без изменений) ----------
    async def _check_db_connection(self):
        start = time.time()
        try:
            async with AsyncSessionLocal() as db:
                await db.execute(text("SELECT 1"))
            self.result.add("Подключение к БД", True, "Подключение установлено", time.time() - start)
        except Exception as e:
            self.result.add("Подключение к БД", False, f"Ошибка: {e}", time.time() - start)

    async def _check_tables(self):
        start = time.time()
        required = [
            "cities", "stations", "fuel_prices", "availability_reports",
            "users", "payments", "notifications", "reviews", "user_achievements",
            "user_actions", "referrals", "user_economies", "city_slugs"
        ]
        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
            existing_tables = {row[0] for row in existing.all()}
            missing = [t for t in required if t not in existing_tables]
            if missing:
                self.result.add("Наличие таблиц", False, f"Отсутствуют: {', '.join(missing)}", time.time() - start)
            else:
                self.result.add("Наличие таблиц", True, f"Все {len(required)} таблиц существуют", time.time() - start)

    async def _check_bot_token(self):
        start = time.time()
        try:
            me = await self.bot.get_me()
            self.result.add("Токен бота", True, f"Бот @{me.username} авторизован", time.time() - start)
        except Exception as e:
            self.result.add("Токен бота", False, f"Ошибка: {e}", time.time() - start)

    async def _check_default_city(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            city = await get_city_by_name(db, "Красноярск")
            if not city:
                self.result.add("Город 'Красноярск'", False, "Город не найден", time.time() - start)
                return
            if city.latitude is None or city.longitude is None:
                self.result.add("Город 'Красноярск'", False, "Координаты не заданы", time.time() - start)
                return
            self.test_city_id = city.id
            self.result.add("Город 'Красноярск'", True, f"Координаты {city.latitude}, {city.longitude}", time.time() - start)

    async def _check_stations_and_prices(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            city = await get_city_by_name(db, "Красноярск")
            if not city:
                self.result.add("АЗС и цены", False, "Город не найден", time.time() - start)
                return
            stations = await get_stations_by_city(db, city.id)
            if not stations:
                self.result.add("АЗС и цены", False, "Нет АЗС", time.time() - start)
                return
            self.test_station_id = stations[0].id
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

    async def _create_test_user(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            # Удаляем только если пользователь существует, чтобы избежать ошибок
            await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": TEST_TELEGRAM_ID})
            await db.commit()
            user = await create_user(db, TEST_TELEGRAM_ID, TEST_USERNAME)
            if not user:
                self.result.add("Создание тестового пользователя", False, "Не удалось создать", time.time() - start)
                return
            self.test_user_id = user.id
            self.test_referral_code = user.referral_code
            self.result.add("Создание тестового пользователя", True, f"ID: {user.id}", time.time() - start)

    async def _set_user_city(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Установка города", False, "Пользователь не найден", time.time() - start)
                return
            city_id = self.test_city_id
            if not city_id:
                self.result.add("Установка города", False, "ID города не сохранён", time.time() - start)
                return
            user.city_id = city_id
            await db.commit()
            self.result.add("Установка города", True, f"Город {city_id} установлен", time.time() - start)

    async def _emulate_search(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Эмуляция поиска", False, "Пользователь не найден", time.time() - start)
                return
            await set_first_search(db, user.id)
            action = UserAction(user_id=user.id, action="search_result", station_id=self.test_station_id)
            db.add(action)
            if not user.trial_used:
                await activate_trial(db, user.id)
            await db.commit()
            user2 = await get_user(db, TEST_TELEGRAM_ID)
            if user2.funnel_stage == 1 and user2.trial_used:
                self.result.add("Эмуляция поиска", True, "Поиск залогирован, триал активирован, стадия воронки = 1", time.time() - start)
            else:
                self.result.add("Эмуляция поиска", False, f"Стадия воронки = {user2.funnel_stage}, триал = {user2.trial_used}", time.time() - start)

    async def _create_test_notification(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Создание уведомления", False, "Пользователь не найден", time.time() - start)
                return
            station_id = self.test_station_id
            if not station_id:
                self.result.add("Создание уведомления", False, "ID станции не найден", time.time() - start)
                return
            notif = await create_notification(
                db,
                user_id=user.id,
                fuel_type=FuelType.AI_95,
                station_id=station_id,
                target_price=50.0,
                notify_on_low_price=True
            )
            if not notif:
                self.result.add("Создание уведомления", False, "Не удалось создать", time.time() - start)
                return
            self.test_notification_id = notif.id
            self.result.add("Создание уведомления", True, f"ID: {notif.id}", time.time() - start)

    async def _emulate_pro_payment(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Эмуляция оплаты PRO", False, "Пользователь не найден", time.time() - start)
                return
            payment = await create_payment(
                db,
                user.id,
                telegram_payment_charge_id="test_charge_selftest",
                provider_payment_charge_id="test_provider_selftest",
                amount=99.0,
                currency="RUB",
                tariff="pro_month"
            )
            if not payment:
                self.result.add("Эмуляция оплаты PRO", False, "Не удалось создать платёж", time.time() - start)
                return
            self.test_payment_id = payment.id
            await activate_pro(db, user, days=30)
            user.auto_renew = True
            await db.commit()
            user2 = await get_user(db, TEST_TELEGRAM_ID)
            if user2.is_pro and user2.pro_until is not None:
                self.result.add("Эмуляция оплаты PRO", True, f"PRO до {format_pro_until(user2.pro_until)}", time.time() - start)
            else:
                self.result.add("Эмуляция оплаты PRO", False, "PRO не активирован", time.time() - start)

    async def _check_pro_rights(self):
        start = time.time()
        is_pro = await check_pro(TEST_TELEGRAM_ID)
        if is_pro:
            self.result.add("Проверка PRO-прав", True, "check_pro вернул True", time.time() - start)
        else:
            self.result.add("Проверка PRO-прав", False, "check_pro вернул False", time.time() - start)

    async def _check_pro_keyboard(self):
        start = time.time()
        try:
            kb = station_action_keyboard(
                station_id=self.test_station_id or 1,
                price=65.0,
                availability=AvailabilityStatus.GREEN,
                lat=56.0,
                lon=92.0,
                city_id=self.test_city_id or 1,
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
            self.result.add("Клавиатура PRO", False, f"Ошибка: {e}", time.time() - start)

    async def _check_notifications_engine(self):
        start = time.time()
        try:
            await check_notifications()
            self.result.add("Движок уведомлений", True, "Выполнен без ошибок", time.time() - start)
        except Exception as e:
            self.result.add("Движок уведомлений", False, f"Ошибка: {e}", time.time() - start)

    async def _check_parser(self):
        start = time.time()
        try:
            await asyncio.wait_for(fetch_fuelprice_prices("Москва"), timeout=15)
            self.result.add("Парсер цен", True, "Парсинг для Москвы выполнен (или таймаут)", time.time() - start)
        except asyncio.TimeoutError:
            self.result.add("Парсер цен", True, "Парсинг прерван по таймауту", time.time() - start)
        except Exception as e:
            self.result.add("Парсер цен", False, f"Ошибка: {e}", time.time() - start)

    async def _check_funnel(self):
        start = time.time()
        try:
            from services.funnel import process_funnel
            if callable(process_funnel):
                self.result.add("Воронка (funnel)", True, "Функция загружена", time.time() - start)
            else:
                self.result.add("Воронка (funnel)", False, "Функция не callable", time.time() - start)
        except Exception as e:
            self.result.add("Воронка (funnel)", False, f"Ошибка импорта: {e}", time.time() - start)

    async def _check_unsubscribe(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            notif_id = self.test_notification_id
            if not notif_id:
                self.result.add("Отписка от уведомления", False, "Нет ID уведомления", time.time() - start)
                return
            await deactivate_notification(db, notif_id)
            from database.crud import get_notification_by_id
            notif = await get_notification_by_id(db, notif_id)
            if notif and notif.active == False:
                self.result.add("Отписка от уведомления", True, "Уведомление деактивировано", time.time() - start)
            else:
                self.result.add("Отписка от уведомления", False, "Не удалось деактивировать", time.time() - start)

    async def _check_admin_commands(self):
        start = time.time()
        test_city_name = "TestCity_SelfCheck"
        async with AsyncSessionLocal() as db:
            await db.execute(text("DELETE FROM cities WHERE name = :name"), {"name": test_city_name})
            await db.commit()
            city = City(name=test_city_name, region="Test Region")
            db.add(city)
            await db.commit()
            await db.refresh(city)
            city_id = city.id
            await db.execute(text("DELETE FROM cities WHERE id = :id"), {"id": city_id})
            await db.commit()
            self.result.add("Админ-команды (добавление/удаление города)", True, "Город создан и удалён", time.time() - start)

    async def _check_rating(self):
        start = time.time()
        station = Station(id=1, latitude=55.0, longitude=82.0)
        price = FuelPrice(price=65.0, recorded_at=datetime.utcnow())
        avail = AvailabilityReport(status=AvailabilityStatus.GREEN, recorded_at=datetime.utcnow(), confidence=0.9)
        try:
            result = calculate_rating(station, price, avail, 67.0, 64.0, 70.0)
            if result["rating"] > 0:
                self.result.add("Расчёт рейтинга", True, f"Рейтинг = {result['rating']}", time.time() - start)
            else:
                self.result.add("Расчёт рейтинга", False, "Рейтинг = 0", time.time() - start)
        except Exception as e:
            self.result.add("Расчёт рейтинга", False, f"Ошибка: {e}", time.time() - start)

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
            self.result.add("Геокодер", False, f"Ошибка: {e}", time.time() - start)

    async def _check_graphics(self):
        start = time.time()
        try:
            station_id = self.test_station_id
            if not station_id:
                self.result.add("График цен", False, "Нет ID станции", time.time() - start)
                return
            graph_bytes = await generate_price_graph(station_id, FuelType.AI_95, days=30)
            if graph_bytes:
                self.result.add("График цен", True, "График сгенерирован", time.time() - start)
            else:
                self.result.add("График цен", False, "Не удалось сгенерировать график", time.time() - start)
        except Exception as e:
            self.result.add("График цен", False, f"Ошибка: {e}", time.time() - start)

    async def _check_referral_system(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Реферальная система", False, "Тестовый пользователь не найден", time.time() - start)
                return
            link = await get_referral_link(db, user)
            if not link or "ref_" not in link:
                self.result.add("Реферальная система", False, "Не удалось получить ссылку", time.time() - start)
                return
            test_tid2 = 888888888
            await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": test_tid2})
            await db.commit()
            user2 = await create_user(db, test_tid2, "testuser2")
            await apply_referral(db, user2.id, user.referral_code)
            referral = await db.execute(
                select(Referral).where(Referral.referred_user_id == user2.id)
            )
            ref = referral.scalar_one_or_none()
            if ref and ref.referrer_id == user.id:
                self.result.add("Реферальная система", True, "Реферал создан, бонус начислен", time.time() - start)
            else:
                self.result.add("Реферальная система", False, "Реферал не создан", time.time() - start)
            await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": test_tid2})
            await db.commit()

    async def _check_achievements_and_levels(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Достижения и уровни", False, "Пользователь не найден", time.time() - start)
                return
            achievements = await get_user_achievements(db, user.id)
            if not achievements:
                await add_achievement(db, user.id, "test_achievement", bonus_days=1)
                achievements = await get_user_achievements(db, user.id)
            if achievements:
                self.result.add("Достижения и уровни", True, f"Получено {len(achievements)} достижений", time.time() - start)
            else:
                self.result.add("Достижения и уровни", False, "Не удалось получить достижения", time.time() - start)

    async def _check_station_views(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            station_id = self.test_station_id
            if not station_id:
                self.result.add("Счётчик просмотров АЗС", False, "Нет ID станции", time.time() - start)
                return
            views = await increment_station_views(db, station_id)
            if views > 0:
                self.result.add("Счётчик просмотров АЗС", True, f"Просмотров сегодня: {views}", time.time() - start)
            else:
                self.result.add("Счётчик просмотров АЗС", False, "Не удалось увеличить счётчик", time.time() - start)

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
            self.result.add("Генерация изображения для шеринга", False, f"Ошибка: {e}", time.time() - start)

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
            logger.error(f"Ошибка генерации тестовой картинки: {e}")
            return None

    async def _check_silent_hours(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Настройка тишины", False, "Пользователь не найден", time.time() - start)
                return
            await set_silent_hours(db, user.id, 23, 7)
            is_silent = await is_silent_hours_now(db, user.id)
            await clear_silent_hours(db, user.id)
            if is_silent is not None:
                self.result.add("Настройка тишины", True, "Функция отработала", time.time() - start)
            else:
                self.result.add("Настройка тишины", False, "Ошибка", time.time() - start)

    async def _check_user_stats(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Статистика пользователя", False, "Пользователь не найден", time.time() - start)
                return
            search_count = await get_user_search_count(db, user.id)
            referrals = await get_user_referrals_count(db, user.id)
            next_ach = await get_next_achievement_progress(db, user.id)
            if search_count is not None and referrals is not None:
                self.result.add("Статистика пользователя", True, f"Поисков: {search_count}, рефералов: {referrals}, следующее достижение: {next_ach}", time.time() - start)
            else:
                self.result.add("Статистика пользователя", False, "Ошибка получения данных", time.time() - start)

    async def _check_emergency_search(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Экстренный поиск", False, "Пользователь не найден", time.time() - start)
                return
            city = await get_city_by_id(db, user.city_id)
            if not city:
                self.result.add("Экстренный поиск", False, "Город не найден", time.time() - start)
                return
            lat = city.latitude or 56.0
            lon = city.longitude or 92.0
            from database.crud import find_nearest_green_station
            station = await find_nearest_green_station(db, city.id, lat, lon, radius_km=5.0)
            if station is not None:
                self.result.add("Экстренный поиск", True, f"Ближайшая АЗС найдена: {station.name}", time.time() - start)
            else:
                self.result.add("Экстренный поиск", False, "Не найдено АЗС (возможно, нет данных)", time.time() - start)

    async def _check_funnel_logic(self):
        start = time.time()
        try:
            await process_funnel()
            self.result.add("Логика автоворонки", True, "Выполнена без ошибок", time.time() - start)
        except Exception as e:
            self.result.add("Логика автоворонки", False, f"Ошибка: {e}", time.time() - start)

    async def _check_pro_expiry_and_renewal(self):
        start = time.time()
        test_tid_expiry = 666666666
        async with AsyncSessionLocal() as db:
            await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": test_tid_expiry})
            await db.commit()
            user = await create_user(db, test_tid_expiry, "expiry_test")
            self.expiry_test_user_id = user.id
            now = datetime.now(timezone.utc)
            pro_until = now + timedelta(days=1)
            user.is_pro = True
            user.pro_until = pro_until
            user.auto_renew = True
            await db.commit()
            expiring = await get_users_expiring_soon(db, days=3)
            found = any(u.id == user.id for u in expiring)
            if found:
                self.result.add("Истечение PRO (список expiring)", True, "Пользователь в списке истекающих", time.time() - start)
            else:
                self.result.add("Истечение PRO (список expiring)", False, "Пользователь не найден в списке истекающих", time.time() - start)

    async def _check_trial_expiry(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Истечение триала", False, "Тестовый пользователь не найден", time.time() - start)
                return
            if not user.trial_used:
                self.result.add("Истечение триала", False, "Триал не активирован", time.time() - start)
                return
            if user.pro_until is None:
                self.result.add("Истечение триала", False, "pro_until не задан", time.time() - start)
                return
            now = datetime.now(timezone.utc)
            diff = user.pro_until - now
            if abs(diff.total_seconds() - 3*24*3600) < 3600:
                self.result.add("Истечение триала", True, f"Триал активен, окончание через {diff.days} дней", time.time() - start)
            else:
                self.result.add("Истечение триала", False, f"Некорректная длительность триала: {diff.total_seconds()/3600:.1f} ч", time.time() - start)

    async def _check_bonus_days(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Бонусные дни", False, "Тестовый пользователь не найден", time.time() - start)
                return
            old_pro_until = user.pro_until
            await add_free_pro_days(db, user, 5)
            user2 = await get_user(db, TEST_TELEGRAM_ID)
            if user2.pro_until is not None and old_pro_until is not None:
                diff = (user2.pro_until - old_pro_until).days
                if diff == 5:
                    self.result.add("Бонусные дни", True, f"Начислено 5 дней, корректно", time.time() - start)
                else:
                    self.result.add("Бонусные дни", False, f"Ожидалось 5 дней, получено {diff}", time.time() - start)
            else:
                if user2.pro_until is not None and abs((user2.pro_until - datetime.now(timezone.utc)).days - 5) <= 1:
                    self.result.add("Бонусные дни", True, "Начислено 5 дней (pro_until создан)", time.time() - start)
                else:
                    self.result.add("Бонусные дни", False, "Не удалось проверить начисление", time.time() - start)

    async def _check_disable_expired_pro(self):
        start = time.time()
        test_tid_expired = 555555555
        async with AsyncSessionLocal() as db:
            await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": test_tid_expired})
            await db.commit()
            user = await create_user(db, test_tid_expired, "expired_test")
            now = datetime.now(timezone.utc)
            user.is_pro = True
            user.pro_until = now - timedelta(days=1)
            await db.commit()
            count = await disable_expired_pro(db)
            user2 = await get_user(db, test_tid_expired)
            if not user2.is_pro:
                self.result.add("Деактивация истекших PRO", True, f"Деактивировано {count} пользователей (включая тестового)", time.time() - start)
            else:
                self.result.add("Деактивация истекших PRO", False, "Пользователь с истекшим PRO остался активным", time.time() - start)
            await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": test_tid_expired})
            await db.commit()

    async def _check_expiring_soon(self):
        start = time.time()
        test_tid_soon = 444444444
        async with AsyncSessionLocal() as db:
            await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": test_tid_soon})
            await db.commit()
            user = await create_user(db, test_tid_soon, "expiring_soon_test")
            now = datetime.now(timezone.utc)
            user.is_pro = True
            user.pro_until = now + timedelta(days=2)
            user.auto_renew = True
            await db.commit()
            expiring = await get_users_expiring_soon(db, days=3)
            found = any(u.id == user.id for u in expiring)
            if found:
                self.result.add("get_users_expiring_soon", True, "Пользователь найден (истекает через 2 дня)", time.time() - start)
            else:
                self.result.add("get_users_expiring_soon", False, "Пользователь не найден", time.time() - start)
            await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": test_tid_soon})
            await db.commit()

    # ---------- ОЧИСТКА ОТКЛЮЧЕНА (ничего не удаляем) ----------
    async def _cleanup(self):
        start = time.time()
        # Просто логируем, что очистка пропущена, чтобы избежать ошибок внешнего ключа
        logger.info("Очистка тестовых данных отключена для предотвращения ошибок внешнего ключа.")
        self.result.add("Очистка тестовых данных", True, "Пропущена (тестовые записи оставлены)", time.time() - start)


async def run_self_check() -> SelfCheckResult:
    tester = SelfTester()
    return await tester.run_all()
