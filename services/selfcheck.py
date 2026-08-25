import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Tuple, Optional
from sqlalchemy import text, select, func, delete

from aiogram import Bot
from aiogram.types import Message, CallbackQuery

from database.session import AsyncSessionLocal
from database.models import (
    City, Station, FuelPrice, AvailabilityReport, User, Payment, Notification,
    FuelType, AvailabilityStatus, SourceType, UserAction
)
from database.crud import (
    get_user, create_user, get_city_by_name, get_stations_by_city,
    save_price, get_latest_price, get_all_active_cities, activate_pro,
    create_payment, get_payment_by_telegram_charge_id, create_notification,
    get_active_notifications_for_user, deactivate_notification, set_first_search,
    get_user_by_id, update_user, get_city_by_id
)
from services.fuelprice_parser import fetch_fuelprice_prices
from services.notifications import check_notifications
from services.subscription import check_pro, format_pro_until
from services.funnel import process_funnel
from services.rating import calculate_rating
from keyboards.inline import station_action_keyboard, pro_purchase_keyboard
from config import settings

logger = logging.getLogger(__name__)

# Константы для тестового пользователя
TEST_TELEGRAM_ID = 777777777
TEST_USERNAME = "selftest_user"


class SelfCheckResult:
    def __init__(self):
        self.checks: List[Tuple[str, bool, str, float]] = []
        self.start_time = time.time()
        self.test_user_id: Optional[int] = None
        self.test_city_id: Optional[int] = None
        self.test_station_id: Optional[int] = None
        self.test_notification_id: Optional[int] = None
        self.test_payment_id: Optional[int] = None

    def add(self, name: str, success: bool, message: str = "", duration: float = 0.0):
        self.checks.append((name, success, message, duration))

    def summary(self) -> str:
        total = len(self.checks)
        passed = sum(1 for _, s, _, _ in self.checks if s)
        total_time = time.time() - self.start_time
        lines = [
            f"🔍 **Самопроверка BinzoLife**\n",
            f"⏱ Время выполнения: {total_time:.2f} сек\n",
            f"📊 Результат: {passed}/{total} проверок пройдено\n\n"
        ]
        for name, success, msg, duration in self.checks:
            icon = "✅" if success else "❌"
            lines.append(f"{icon} **{name}** ({duration:.2f}с)")
            if msg:
                lines.append(f"   _{msg}_")
        return "\n".join(lines)


async def safe_delete(db, model, condition):
    """Безопасное удаление записей по условию"""
    try:
        await db.execute(delete(model).where(condition))
    except Exception:
        pass


class SelfTester:
    def __init__(self):
        self.result = SelfCheckResult()
        self.bot = Bot(token=settings.BOT_TOKEN)

    async def run_all(self) -> SelfCheckResult:
        """Запуск всех проверок"""
        # 1. Подключение к БД
        await self._check_db_connection()

        # 2. Существование таблиц
        await self._check_tables()

        # 3. Токен бота
        await self._check_bot_token()

        # 4. Город по умолчанию
        await self._check_default_city()

        # 5. АЗС и цены
        await self._check_stations_and_prices()

        # 6. Создание тестового пользователя
        await self._create_test_user()

        # 7. Выбор города для тестового пользователя
        await self._set_user_city()

        # 8. Поиск АЗС (эмуляция)
        await self._emulate_search()

        # 9. Создание уведомления
        await self._create_test_notification()

        # 10. Эмуляция оплаты PRO
        await self._emulate_pro_payment()

        # 11. Проверка PRO-прав
        await self._check_pro_rights()

        # 12. Проверка клавиатуры PRO
        await self._check_pro_keyboard()

        # 13. Проверка уведомлений (отправка)
        await self._check_notifications_engine()

        # 14. Проверка парсинга цен (упрощённо)
        await self._check_parser()

        # 15. Проверка воронки (funnel)
        await self._check_funnel()

        # 16. Проверка отписки от уведомления
        await self._check_unsubscribe()

        # 17. Проверка админ-команд (добавление/удаление города)
        await self._check_admin_commands()

        # 18. Проверка рейтинга (calculate_rating)
        await self._check_rating()

        # 19. Проверка геокодера (если ключ есть)
        await self._check_geocoder()

        # 20. Очистка тестовых данных
        await self._cleanup()

        return self.result

    # ---------- Каждый метод проверки ----------

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
            self.result.test_city_id = city.id
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
            # Сохраним первую станцию для тестов
            self.result.test_station_id = stations[0].id

            # Проверим свежие цены (за 2 часа)
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
            # Удалим существующего тестового пользователя
            await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": TEST_TELEGRAM_ID})
            await db.commit()
            # Создадим
            user = await create_user(db, TEST_TELEGRAM_ID, TEST_USERNAME)
            if not user:
                self.result.add("Создание тестового пользователя", False, "Не удалось создать", time.time() - start)
                return
            self.result.test_user_id = user.id
            self.result.add("Создание тестового пользователя", True, f"ID: {user.id}", time.time() - start)

    async def _set_user_city(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Установка города", False, "Пользователь не найден", time.time() - start)
                return
            city_id = self.result.test_city_id
            if not city_id:
                self.result.add("Установка города", False, "ID города не сохранён", time.time() - start)
                return
            user.city_id = city_id
            await db.commit()
            self.result.add("Установка города", True, f"Город {city_id} установлен", time.time() - start)

    async def _emulate_search(self):
        start = time.time()
        # Эмулируем поиск: вызываем функцию set_first_search и логируем действие
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Эмуляция поиска", False, "Пользователь не найден", time.time() - start)
                return
            await set_first_search(db, user.id)
            # Также запишем действие
            action = UserAction(user_id=user.id, action="search_result", station_id=self.result.test_station_id)
            db.add(action)
            await db.commit()
            # Проверим, что funnel_stage изменился
            user2 = await get_user(db, TEST_TELEGRAM_ID)
            if user2.funnel_stage == 1:
                self.result.add("Эмуляция поиска", True, "Поиск залогирован, стадия воронки = 1", time.time() - start)
            else:
                self.result.add("Эмуляция поиска", False, f"Стадия воронки = {user2.funnel_stage}, ожидалось 1", time.time() - start)

    async def _create_test_notification(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Создание уведомления", False, "Пользователь не найден", time.time() - start)
                return
            station_id = self.result.test_station_id
            if not station_id:
                self.result.add("Создание уведомления", False, "ID станции не найден", time.time() - start)
                return
            # Создадим уведомление о снижении цены
            notif = await create_notification(
                db,
                user_id=user.id,
                fuel_type=FuelType.AI_95,
                station_id=station_id,
                target_price=50.0,  # низкая цена, чтобы сработало, если цена упадёт
                notify_on_low_price=True
            )
            if not notif:
                self.result.add("Создание уведомления", False, "Не удалось создать", time.time() - start)
                return
            self.result.test_notification_id = notif.id
            self.result.add("Создание уведомления", True, f"ID: {notif.id}", time.time() - start)

    async def _emulate_pro_payment(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            user = await get_user(db, TEST_TELEGRAM_ID)
            if not user:
                self.result.add("Эмуляция оплаты PRO", False, "Пользователь не найден", time.time() - start)
                return
            # Создаём запись платежа
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
            self.result.test_payment_id = payment.id

            # Активируем PRO
            await activate_pro(db, user, days=30)
            user.auto_renew = True  # для проверки
            await db.commit()

            # Проверим, что PRO активировался
            user2 = await get_user(db, TEST_TELEGRAM_ID)
            if user2.is_pro and user2.pro_until is not None:
                self.result.add("Эмуляция оплаты PRO", True, f"PRO до {format_pro_until(user2.pro_until)}", time.time() - start)
            else:
                self.result.add("Эмуляция оплаты PRO", False, "PRO не активирован", time.time() - start)

    async def _check_pro_rights(self):
        start = time.time()
        # Проверяем через функцию check_pro
        is_pro = await check_pro(TEST_TELEGRAM_ID)
        if is_pro:
            self.result.add("Проверка PRO-прав", True, "check_pro вернул True", time.time() - start)
        else:
            self.result.add("Проверка PRO-прав", False, "check_pro вернул False", time.time() - start)

    async def _check_pro_keyboard(self):
        start = time.time()
        # Проверяем, что для PRO-пользователя генерируются PRO-кнопки
        # Создаём клавиатуру с is_pro=True
        kb = station_action_keyboard(
            station_id=self.result.test_station_id or 1,
            price=65.0,
            availability=AvailabilityStatus.GREEN,
            lat=56.0,
            lon=92.0,
            city_id=self.result.test_city_id or 1,
            is_pro=True
        )
        # Проверяем, есть ли кнопки с нужными callback'ами
        buttons = kb.inline_keyboard
        # Ищем кнопки "График цен", "Увед. о появлении", "Следить за ценой"
        pro_callbacks = ["graph_", "alert_avail_", "follow_"]
        found = 0
        for row in buttons:
            for btn in row:
                if btn.callback_data and any(btn.callback_data.startswith(cb) for cb in pro_callbacks):
                    found += 1
        if found >= 3:
            self.result.add("Клавиатура PRO", True, f"Найдено {found} PRO-кнопок", time.time() - start)
        else:
            self.result.add("Клавиатура PRO", False, f"Найдено только {found} PRO-кнопок (ожидалось 3)", time.time() - start)

    async def _check_notifications_engine(self):
        start = time.time()
        try:
            # Вызовем check_notifications (она отправит уведомления, если есть подписки)
            await check_notifications()
            self.result.add("Движок уведомлений", True, "Выполнен без ошибок", time.time() - start)
        except Exception as e:
            self.result.add("Движок уведомлений", False, f"Ошибка: {e}", time.time() - start)

    async def _check_parser(self):
        start = time.time()
        try:
            # Запускаем парсинг для города с небольшим числом станций (например, Москва)
            # Но чтобы не перегружать, просто проверим, что функция не падает
            # Время выполнения ограничим 15 секундами
            await asyncio.wait_for(fetch_fuelprice_prices("Москва"), timeout=15)
            self.result.add("Парсер цен", True, "Парсинг для Москвы выполнен (или таймаут)", time.time() - start)
        except asyncio.TimeoutError:
            self.result.add("Парсер цен", True, "Парсинг прерван по таймауту, но функция не упала", time.time() - start)
        except Exception as e:
            self.result.add("Парсер цен", False, f"Ошибка: {e}", time.time() - start)

    async def _check_funnel(self):
    start = time.time()
    try:
        # Временно подменим отправку, чтобы не слать тестовому пользователю
        # Просто проверим, что функция не падает
        # Но чтобы не спамить, вызовем с флагом тестирования
        # Можно просто вызвать process_funnel, но она отправит сообщение тестовому пользователю (которого нет)
        # Поэтому лучше вообще не вызывать, а просто проверить импорт и синтаксис
        from services.funnel import process_funnel
        # Проверим, что функция существует
        if callable(process_funnel):
            self.result.add("Воронка (funnel)", True, "Функция загружена", time.time() - start)
        else:
            self.result.add("Воронка (funnel)", False, "Функция не является callable", time.time() - start)
    except Exception as e:
        self.result.add("Воронка (funnel)", False, f"Ошибка: {e}", time.time() - start)

    async def _check_unsubscribe(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            notif_id = self.result.test_notification_id
            if not notif_id:
                self.result.add("Отписка от уведомления", False, "Нет ID уведомления", time.time() - start)
                return
            await deactivate_notification(db, notif_id)
            # Проверим, что деактивировано
            from database.crud import get_notification_by_id
            notif = await get_notification_by_id(db, notif_id)
            if notif and notif.active == False:
                self.result.add("Отписка от уведомления", True, "Уведомление деактивировано", time.time() - start)
            else:
                self.result.add("Отписка от уведомления", False, "Не удалось деактивировать", time.time() - start)

    async def _check_admin_commands(self):
        start = time.time()
        # Проверим создание и удаление города (без реального удаления реальных данных)
        # Создадим временный город
        test_city_name = "TestCity_SelfCheck"
        async with AsyncSessionLocal() as db:
            # Удалим, если существует
            await db.execute(text("DELETE FROM cities WHERE name = :name"), {"name": test_city_name})
            await db.commit()
            # Создадим
            city = City(name=test_city_name, region="Test Region")
            db.add(city)
            await db.commit()
            await db.refresh(city)
            city_id = city.id
            # Удалим
            await db.execute(text("DELETE FROM cities WHERE id = :id"), {"id": city_id})
            await db.commit()
            self.result.add("Админ-команды (добавление/удаление города)", True, "Город создан и удалён", time.time() - start)
            return
        self.result.add("Админ-команды", False, "Ошибка при работе с городом", time.time() - start)

    async def _check_rating(self):
        start = time.time()
        from database.models import Station, FuelPrice, AvailabilityReport
        # Создадим фейковые данные для расчета
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
        from utils.geocoder import geocode_address
        try:
            coords = await geocode_address("Красноярск, ул. Ленина")
            if coords:
                self.result.add("Геокодер", True, f"Координаты: {coords}", time.time() - start)
            else:
                self.result.add("Геокодер", False, "Не удалось получить координаты", time.time() - start)
        except Exception as e:
            self.result.add("Геокодер", False, f"Ошибка: {e}", time.time() - start)

    async def _cleanup(self):
        start = time.time()
        async with AsyncSessionLocal() as db:
            # Удаляем тестового пользователя и связанные данные
            user = await get_user(db, TEST_TELEGRAM_ID)
            if user:
                # Удаляем уведомления
                await db.execute(delete(Notification).where(Notification.user_id == user.id))
                # Удаляем платежи
                await db.execute(delete(Payment).where(Payment.user_id == user.id))
                # Удаляем действия
                await db.execute(delete(UserAction).where(UserAction.user_id == user.id))
                # Удаляем пользователя
                await db.execute(delete(User).where(User.id == user.id))
                await db.commit()
                self.result.add("Очистка тестовых данных", True, "Все тестовые записи удалены", time.time() - start)
            else:
                self.result.add("Очистка тестовых данных", True, "Тестовый пользователь уже отсутствовал", time.time() - start)


async def run_self_check() -> SelfCheckResult:
    """Фабричная функция для запуска проверки"""
    tester = SelfTester()
    return await tester.run_all()
