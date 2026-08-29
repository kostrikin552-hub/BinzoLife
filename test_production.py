#!/usr/bin/env python3
"""
Производственный тест BinzoLife – полная проверка с детальным логированием.
Возвращает (success, message) для каждой проверки.
"""
import asyncio
import os
import sys
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple, List
import aiohttp
import traceback

# Настройка логов
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("production_test")

# Добавляем путь к проекту
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Импорты проекта
from config import settings
from database.session import AsyncSessionLocal, engine
from database.models import Base, City, Station, User, CitySlug, ProNotificationSent, Payment, Referral, UserAchievement
from database.crud import (
    get_user, create_user, can_use_free_search, use_free_search,
    get_all_active_cities, get_stations_by_city, get_city_slug,
    create_payment, activate_pro, is_user_pro, get_user_by_id,
    add_free_pro_days, activate_trial, get_users_expiring_soon,
    disable_expired_pro, apply_referral, get_referral_link,
    add_achievement, get_user_achievements
)
from services.fuelprice_parser import fetch_fuelprice_prices
from utils.geocoder import geocode_address
from sqlalchemy import text
from aiogram import Bot
from aiogram.types import LabeledPrice

# ------------------ Вспомогательные функции ------------------
async def create_test_user(db) -> Tuple[Optional[User], str]:
    """Создаёт тестового пользователя с уникальным telegram_id. Возвращает (user, message)."""
    import random
    test_telegram_id = -3000000 - random.randint(1, 100000)
    try:
        # Удаляем, если существует
        await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": test_telegram_id})
        await db.commit()
        user = await create_user(db, test_telegram_id, "test_user")
        return user, f"Пользователь создан с ID {user.id}, telegram_id {test_telegram_id}"
    except Exception as e:
        return None, f"Ошибка создания пользователя: {str(e)}"

async def cleanup_test_user(db, user: User) -> str:
    """Удаляет тестового пользователя. Возвращает сообщение."""
    try:
        await db.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user.id})
        await db.commit()
        return f"Пользователь {user.id} удалён"
    except Exception as e:
        return f"Ошибка удаления пользователя: {str(e)}"

# ------------------ Проверки (возвращают (bool, message)) ------------------

async def check_env_vars() -> Tuple[bool, str]:
    required = ["BOT_TOKEN", "ADMIN_ID", "DATABASE_URL", "INTERNAL_TOKEN"]
    missing = []
    warnings = []
    for var in required:
        if not getattr(settings, var, None):
            missing.append(var)
    if missing:
        return False, f"Отсутствуют обязательные переменные: {', '.join(missing)}"
    if settings.INTERNAL_TOKEN in ["ваш_секретный_токен", "secret", "token", "internal_token"]:
        warnings.append("INTERNAL_TOKEN слишком простой")
    if settings.PROVIDER_TOKEN:
        if settings.PROVIDER_TOKEN in ["ваш_провайдер_токен", "test", "provider"]:
            warnings.append("PROVIDER_TOKEN выглядит как заглушка")
    else:
        warnings.append("PROVIDER_TOKEN не задан – рублёвые платежи не будут работать")
    msg = "Все обязательные переменные заданы"
    if warnings:
        msg += " (предупреждения: " + "; ".join(warnings) + ")"
    return True, msg

async def check_db_connection() -> Tuple[bool, str]:
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return True, "Подключение установлено"
    except Exception as e:
        return False, f"Ошибка подключения: {str(e)}"

async def check_db_schema() -> Tuple[bool, str]:
    required_tables = ["cities", "stations", "fuel_prices", "availability_reports",
                       "users", "payments", "notifications", "reviews",
                       "user_achievements", "referrals", "user_economies",
                       "city_slugs", "pro_notifications_sent", "task_locks"]
    try:
        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
            existing_tables = {row[0] for row in existing.all()}
            missing = [t for t in required_tables if t not in existing_tables]
            if missing:
                return False, f"Отсутствуют таблицы: {', '.join(missing)}"
            # Проверяем колонки users
            columns = await db.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name='users'")
            )
            col_names = {row[0] for row in columns.all()}
            required_cols = {"free_searches_today", "last_free_search_date"}
            missing_cols = required_cols - col_names
            if missing_cols:
                return False, f"В таблице users отсутствуют колонки: {', '.join(missing_cols)}"
            # Проверяем наличие city_slugs
            slugs_count = await db.execute(text("SELECT COUNT(*) FROM city_slugs"))
            if slugs_count.scalar() == 0:
                return True, "Все таблицы и колонки есть, но city_slugs пуста (возможно, города не импортированы)"
        return True, "Все таблицы и колонки присутствуют"
    except Exception as e:
        return False, f"Ошибка проверки схемы: {str(e)}"

async def check_cities_and_stations() -> Tuple[bool, str]:
    try:
        async with AsyncSessionLocal() as db:
            cities = await get_all_active_cities(db)
            if not cities:
                return False, "Нет активных городов"
            warnings = []
            for city in cities:
                if city.latitude is None or city.longitude is None:
                    warnings.append(f"Город {city.name} без координат")
                slug = await get_city_slug(db, city.name)
                if not slug:
                    warnings.append(f"Для города {city.name} нет слага")
                stations = await get_stations_by_city(db, city.id)
                if not stations:
                    warnings.append(f"В городе {city.name} нет АЗС")
            fresh_prices = await db.execute(
                text("SELECT COUNT(*) FROM fuel_prices WHERE is_fresh = True")
            )
            if fresh_prices.scalar() == 0:
                warnings.append("Нет свежих цен (is_fresh=True)")
            if warnings:
                return True, f"{len(cities)} городов, но есть предупреждения: " + "; ".join(warnings)
            return True, f"{len(cities)} городов, все в порядке"
    except Exception as e:
        return False, f"Ошибка: {str(e)}"

async def check_parser() -> Tuple[bool, str]:
    test_cities = ["Москва", "Санкт-Петербург", "Красноярск"]
    successes = []
    errors = []
    for city in test_cities:
        try:
            await fetch_fuelprice_prices(city)
            successes.append(city)
        except Exception as e:
            errors.append(f"{city}: {str(e)[:80]}")
    if errors:
        return False, f"Ошибки при парсинге: " + "; ".join(errors)
    return True, f"Успешно загружены {len(successes)} городов: " + ", ".join(successes)

async def check_geocoder() -> Tuple[bool, str]:
    if not settings.YANDEX_GEOCODER_API_KEY:
        return True, "Ключ не задан, проверка пропущена"
    try:
        coords = await geocode_address("Красноярск, ул. Ленина")
        if coords:
            return True, f"Координаты получены: {coords}"
        else:
            return False, "Геокодер вернул None"
    except Exception as e:
        return False, f"Ошибка: {str(e)}"

async def check_free_search_logic() -> Tuple[bool, str]:
    try:
        async with AsyncSessionLocal() as db:
            user, msg = await create_test_user(db)
            if not user:
                return False, f"Не удалось создать пользователя: {msg}"
            # Проверка 1
            can1 = await can_use_free_search(db, user.id)
            if not can1:
                await cleanup_test_user(db, user)
                return False, "Первый вызов can_use_free_search вернул False"
            # Используем поиск
            remaining = await use_free_search(db, user.id)
            if remaining != 0:
                await cleanup_test_user(db, user)
                return False, f"После использования осталось {remaining}, ожидалось 0"
            # Проверка 2
            can2 = await can_use_free_search(db, user.id)
            if can2:
                await cleanup_test_user(db, user)
                return False, "Второй вызов can_use_free_search вернул True, а должен False"
            # Сбрасываем дату
            user.last_free_search_date = date.today() - timedelta(days=1)
            user.free_searches_today = 0
            await db.commit()
            can3 = await can_use_free_search(db, user.id)
            if not can3:
                await cleanup_test_user(db, user)
                return False, "После сброса даты can_use_free_search вернул False"
            await cleanup_test_user(db, user)
            return True, "Все проверки пройдены"
    except Exception as e:
        return False, f"Исключение: {str(e)}\n{traceback.format_exc()}"

async def check_payment_and_pro_activation() -> Tuple[bool, str]:
    try:
        async with AsyncSessionLocal() as db:
            user, msg = await create_test_user(db)
            if not user:
                return False, f"Не удалось создать пользователя: {msg}"
            charge_id = f"test_charge_{user.id}_{int(datetime.now().timestamp())}"
            try:
                payment = await create_payment(
                    db,
                    user_id=user.id,
                    telegram_payment_charge_id=charge_id,
                    provider_payment_charge_id="test_provider",
                    amount=99.0,
                    currency="RUB",
                    tariff="pro_month"
                )
            except Exception as e:
                await cleanup_test_user(db, user)
                return False, f"Ошибка создания платежа: {str(e)}"
            # Активируем PRO
            try:
                await activate_pro(db, user, days=30)
            except Exception as e:
                await cleanup_test_user(db, user)
                return False, f"Ошибка активации PRO: {str(e)}"
            # Проверяем статус
            is_pro = await is_user_pro(db, user)
            if not is_pro:
                await cleanup_test_user(db, user)
                return False, "После активации is_user_pro вернул False"
            if user.pro_until is None:
                await cleanup_test_user(db, user)
                return False, "pro_until не установлен"
            days_left = (user.pro_until - datetime.now(timezone.utc)).days
            if days_left < 28:
                await cleanup_test_user(db, user)
                return False, f"Дата окончания PRO некорректна (осталось {days_left} дней, ожидалось 30)"
            # Проверяем платёж
            payment_check = await db.execute(
                text("SELECT * FROM payments WHERE telegram_payment_charge_id = :cid"),
                {"cid": charge_id}
            )
            if not payment_check.fetchone():
                await cleanup_test_user(db, user)
                return False, "Платёж не найден в БД"
            await cleanup_test_user(db, user)
            return True, "PRO активирован, платёж сохранён"
    except Exception as e:
        return False, f"Исключение: {str(e)}\n{traceback.format_exc()}"

async def check_trial() -> Tuple[bool, str]:
    try:
        async with AsyncSessionLocal() as db:
            user, msg = await create_test_user(db)
            if not user:
                return False, f"Не удалось создать пользователя: {msg}"
            try:
                result = await activate_trial(db, user.id)
            except Exception as e:
                await cleanup_test_user(db, user)
                return False, f"Ошибка активации триала: {str(e)}"
            if not result:
                await cleanup_test_user(db, user)
                return False, "activate_trial вернул False при первом вызове"
            await db.refresh(user)
            is_pro = await is_user_pro(db, user)
            if not is_pro:
                await cleanup_test_user(db, user)
                return False, "После активации триала is_user_pro вернул False"
            if user.pro_until is None:
                await cleanup_test_user(db, user)
                return False, "pro_until не установлен"
            days_left = (user.pro_until - datetime.now(timezone.utc)).days
            if days_left < 2:
                await cleanup_test_user(db, user)
                return False, f"Дата окончания триала некорректна (осталось {days_left} дней)"
            # Повторная активация
            result2 = await activate_trial(db, user.id)
            if result2:
                await cleanup_test_user(db, user)
                return False, "Повторная активация триала вернула True, а должна False"
            await cleanup_test_user(db, user)
            return True, "Триал работает корректно"
    except Exception as e:
        return False, f"Исключение: {str(e)}\n{traceback.format_exc()}"

async def check_pro_expiry_and_deactivation() -> Tuple[bool, str]:
    try:
        async with AsyncSessionLocal() as db:
            user, msg = await create_test_user(db)
            if not user:
                return False, f"Не удалось создать пользователя: {msg}"
            # Устанавливаем PRO с истекшей датой
            user.is_pro = True
            user.pro_until = datetime.now(timezone.utc) - timedelta(days=1)
            user.auto_renew = False
            await db.commit()
            try:
                count = await disable_expired_pro(db)
            except Exception as e:
                await cleanup_test_user(db, user)
                return False, f"Ошибка disable_expired_pro: {str(e)}"
            await db.refresh(user)
            if user.is_pro:
                await cleanup_test_user(db, user)
                return False, "PRO не был деактивирован после истечения"
            if user.pro_until is not None:
                await cleanup_test_user(db, user)
                return False, "pro_until не очищен"
            await cleanup_test_user(db, user)
            return True, f"PRO деактивирован, затронуто {count} пользователей"
    except Exception as e:
        return False, f"Исключение: {str(e)}\n{traceback.format_exc()}"

async def check_referral_system() -> Tuple[bool, str]:
    try:
        async with AsyncSessionLocal() as db:
            referrer, msg1 = await create_test_user(db)
            if not referrer:
                return False, f"Не удалось создать реферера: {msg1}"
            referred, msg2 = await create_test_user(db)
            if not referred:
                await cleanup_test_user(db, referrer)
                return False, f"Не удалось создать приглашённого: {msg2}"
            code = referrer.referral_code
            try:
                success = await apply_referral(db, referred.id, code)
            except Exception as e:
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False, f"Ошибка apply_referral: {str(e)}"
            if not success:
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False, "apply_referral вернул False (возможно, код уже использован или пользователь уже приглашён)"
            # Проверяем создание реферала
            ref_record = await db.execute(
                text("SELECT * FROM referrals WHERE referrer_id = :rid AND referred_user_id = :reid"),
                {"rid": referrer.id, "reid": referred.id}
            )
            if not ref_record.fetchone():
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False, "Реферал не создан в БД"
            # Проверяем бонус рефереру
            await db.refresh(referrer)
            if not referrer.is_pro:
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False, "Реферер не стал PRO после реферала"
            if referrer.pro_until is None:
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False, "pro_until у реферера не установлен"
            days_left = (referrer.pro_until - datetime.now(timezone.utc)).days
            if days_left < 2:
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False, f"Бонусные дни не начислены (осталось {days_left} дней)"
            # Проверяем ссылку
            link = await get_referral_link(db, referrer)
            if not link or "ref_" not in link:
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False, "Реферальная ссылка некорректна"
            await cleanup_test_user(db, referrer)
            await cleanup_test_user(db, referred)
            return True, "Реферал создан, бонус начислен, ссылка работает"
    except Exception as e:
        return False, f"Исключение: {str(e)}\n{traceback.format_exc()}"

async def check_achievements() -> Tuple[bool, str]:
    try:
        async with AsyncSessionLocal() as db:
            user, msg = await create_test_user(db)
            if not user:
                return False, f"Не удалось создать пользователя: {msg}"
            try:
                await add_achievement(db, user.id, "test_achievement", bonus_days=1)
            except Exception as e:
                await cleanup_test_user(db, user)
                return False, f"Ошибка add_achievement: {str(e)}"
            # Проверяем достижение
            achievements = await get_user_achievements(db, user.id)
            if len(achievements) != 1:
                await cleanup_test_user(db, user)
                return False, f"Достижение не сохранилось (найдено {len(achievements)})"
            if achievements[0].bonus_days_granted != 1:
                await cleanup_test_user(db, user)
                return False, f"Бонусные дни не начислены: {achievements[0].bonus_days_granted}"
            await db.refresh(user)
            if user.pro_until is None:
                await cleanup_test_user(db, user)
                return False, "pro_until не установлен после начисления бонуса"
            days_left = (user.pro_until - datetime.now(timezone.utc)).days
            if days_left < 0:
                await cleanup_test_user(db, user)
                return False, f"Бонусные дни не привели к активации PRO (осталось {days_left})"
            await cleanup_test_user(db, user)
            return True, "Достижение сохранено, бонусные дни начислены"
    except Exception as e:
        return False, f"Исключение: {str(e)}\n{traceback.format_exc()}"

# ========== ПРОВЕРКА РЕАЛЬНЫХ ПЛАТЕЖЕЙ ==========
async def check_real_payment() -> Tuple[bool, str]:
    bot = None
    try:
        bot = Bot(token=settings.BOT_TOKEN)
        admin_id = settings.admin_ids[0] if settings.admin_ids else None
        if not admin_id:
            return False, "ADMIN_ID не задан, не могу отправить тестовый инвойс"
        if not settings.PROVIDER_TOKEN:
            return False, "PROVIDER_TOKEN не задан – рублёвые платежи не будут работать"
        # Пытаемся отправить инвойс на 1 рубль
        prices = [LabeledPrice(label="Тестовый платёж (1 рубль)", amount=100)]
        await bot.send_invoice(
            chat_id=admin_id,
            title="Тестовый платёж BinzoLife",
            description="Это тестовый инвойс для проверки работы платежей. Вы можете не оплачивать его.",
            provider_token=settings.PROVIDER_TOKEN,
            currency="RUB",
            prices=prices,
            start_parameter="test_payment",
            payload="test_payment_payload"
        )
        await bot.session.close()
        return True, "Тестовый инвойс отправлен админу. Если вы видите кнопку оплаты – PROVIDER_TOKEN работает."
    except Exception as e:
        error_msg = str(e)
        if bot:
            try:
                await bot.session.close()
            except:
                pass
        if "PROVIDER_TOKEN" in error_msg or "Invalid provider token" in error_msg or "CURRENCY" in error_msg:
            return False, f"PROVIDER_TOKEN невалидный или не активирован: {error_msg[:150]}"
        else:
            return False, f"Ошибка отправки инвойса: {error_msg[:150]}"

async def check_health_endpoint() -> Tuple[bool, str]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://localhost:{settings.PORT}/health", timeout=5) as resp:
                if resp.status == 200:
                    return True, "Доступен локально"
                else:
                    return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, f"Недоступен: {str(e)[:80]}"

async def check_notification_modules() -> Tuple[bool, str]:
    try:
        from services.pro_notifications import send_pro_expiry_notifications_with_bot
        from services.notifications import check_notifications
        return True, "Модули импортируются без ошибок"
    except Exception as e:
        return False, f"Ошибка импорта: {str(e)}"

async def check_internal_token_security() -> Tuple[bool, str]:
    token = settings.INTERNAL_TOKEN
    if token in ["ваш_секретный_токен", "secret", "token", "internal_token", "123456"]:
        return False, "Слишком простой токен – используйте генератор паролей"
    return True, "Токен выглядит надёжным"

async def check_provider_token() -> Tuple[bool, str]:
    if settings.PROVIDER_TOKEN:
        if settings.PROVIDER_TOKEN in ["ваш_провайдер_токен", "test", "provider"]:
            return False, "Задан, но выглядит как заглушка – проверьте его"
        return True, "Задан"
    else:
        return False, "Не задан – рублёвые платежи не будут работать"

# ------------------ Главная функция, возвращающая детальные результаты ------------------
async def run_all_checks() -> Tuple[int, int, List[Tuple[str, bool, str]], List[str]]:
    """Запускает все проверки и возвращает (passed, total, results, warnings)."""
    results = []  # список (название, success, сообщение)
    warnings = []

    # 1. Переменные окружения
    ok, msg = await check_env_vars()
    results.append(("Переменные окружения", ok, msg))
    if "предупреждения" in msg.lower():
        warnings.append(msg)

    # 2. Подключение к БД
    ok, msg = await check_db_connection()
    results.append(("Подключение к БД", ok, msg))
    if not ok:
        # Если нет БД, дальше не имеет смысла, но мы всё равно продолжим для полноты
        pass

    # 3. Схема БД
    ok, msg = await check_db_schema()
    results.append(("Схема БД", ok, msg))
    if not ok:
        warnings.append(msg)

    # 4. Города и АЗС
    ok, msg = await check_cities_and_stations()
    results.append(("Города и АЗС", ok, msg))
    if not ok:
        warnings.append(msg)

    # 5. Логика бесплатных поисков
    ok, msg = await check_free_search_logic()
    results.append(("Логика бесплатных поисков", ok, msg))

    # 6. Оплата и активация PRO (симуляция)
    ok, msg = await check_payment_and_pro_activation()
    results.append(("Оплата и активация PRO (симуляция)", ok, msg))

    # 7. Триал
    ok, msg = await check_trial()
    results.append(("Триал", ok, msg))

    # 8. Истечение PRO
    ok, msg = await check_pro_expiry_and_deactivation()
    results.append(("Истечение PRO", ok, msg))

    # 9. Реферальная система
    ok, msg = await check_referral_system()
    results.append(("Реферальная система", ok, msg))

    # 10. Достижения
    ok, msg = await check_achievements()
    results.append(("Достижения", ok, msg))

    # 11. Парсер цен
    ok, msg = await check_parser()
    results.append(("Парсер цен", ok, msg))

    # 12. Геокодер
    ok, msg = await check_geocoder()
    results.append(("Геокодер", ok, msg))

    # 13. Эндпоинт /health
    ok, msg = await check_health_endpoint()
    results.append(("Эндпоинт /health", ok, msg))

    # 14. Модули уведомлений
    ok, msg = await check_notification_modules()
    results.append(("Модули уведомлений", ok, msg))

    # 15. Безопасность INTERNAL_TOKEN
    ok, msg = await check_internal_token_security()
    results.append(("Безопасность INTERNAL_TOKEN", ok, msg))

    # 16. PROVIDER_TOKEN (наличие)
    ok, msg = await check_provider_token()
    results.append(("PROVIDER_TOKEN (наличие)", ok, msg))

    # 17. РЕАЛЬНЫЕ ПЛАТЕЖИ
    ok, msg = await check_real_payment()
    results.append(("Реальные платежи (тестовый инвойс)", ok, msg))

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    return passed, total, results, warnings

async def run_all_checks_and_log():
    """Запускает проверки и логирует результат (для использования в командной строке)."""
    passed, total, results, warnings = await run_all_checks()

    logger.info("=" * 60)
    logger.info(f"📊 Результат: {passed}/{total} автоматических проверок пройдено")

    for name, ok, msg in results:
        icon = "✅" if ok else "❌"
        logger.info(f"{icon} {name}: {msg}")

    if warnings:
        logger.warning("⚠️ Предупреждения (не критичны, но стоит обратить внимание):")
        for w in warnings:
            logger.warning(f"   • {w}")

    logger.info("=" * 60)
    logger.info("🔍 Рекомендации для ручного тестирования:")
    logger.info("   • Проверьте, что тестовый инвойс пришёл админу – если да, то PROVIDER_TOKEN работает")
    logger.info("   • Оплатите 1 рубль или 1 Star, чтобы убедиться, что PRO активируется")
    logger.info("   • Проверьте работу cron-заданий (цены, уведомления)")
    logger.info("   • Проверьте, что /health пингуется каждые 5-10 минут")
    logger.info("   • Проверьте логи на наличие ошибок в pro_expiry_notifier")
    logger.info("   • Протестируйте сценарий «Бензин заканчивается!»")

    if passed == total:
        logger.info("🎉 Все автоматические проверки успешны! Бот технически готов к продакшену.")
    else:
        logger.warning("⚠️ Некоторые автоматические проверки не пройдены. Исправьте ошибки перед запуском.")

if __name__ == "__main__":
    asyncio.run(run_all_checks_and_log())
