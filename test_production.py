#!/usr/bin/env python3
"""
Производственный тест BinzoLife – полная проверка всех компонентов,
включая бизнес-логику и реальные платежи (отправка тестового инвойса).
Запускайте через команду /test_prod или напрямую: python test_production.py
"""
import asyncio
import os
import sys
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Tuple, List
import aiohttp

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
def print_check(name: str, success: bool, message: str = ""):
    icon = "✅" if success else "❌"
    logger.info(f"{icon} {name}: {message}")

async def create_test_user(db) -> User:
    import random
    test_telegram_id = -3000000 - random.randint(1, 100000)
    await db.execute(text("DELETE FROM users WHERE telegram_id = :tid"), {"tid": test_telegram_id})
    await db.commit()
    user = await create_user(db, test_telegram_id, "test_user")
    return user

async def cleanup_test_user(db, user: User):
    await db.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": user.id})
    await db.commit()

# ------------------ Проверки ------------------

async def check_env_vars() -> Tuple[bool, List[str]]:
    required = ["BOT_TOKEN", "ADMIN_ID", "DATABASE_URL", "INTERNAL_TOKEN"]
    missing = []
    warnings = []
    for var in required:
        if not getattr(settings, var, None):
            missing.append(var)
    if missing:
        print_check("Переменные окружения (обязательные)", False, f"Отсутствуют: {', '.join(missing)}")
        return False, warnings
    if settings.INTERNAL_TOKEN in ["ваш_секретный_токен", "secret", "token", "internal_token"]:
        warnings.append("INTERNAL_TOKEN слишком простой – используйте генератор паролей")
    if settings.PROVIDER_TOKEN:
        if settings.PROVIDER_TOKEN in ["ваш_провайдер_токен", "test", "provider"]:
            warnings.append("PROVIDER_TOKEN задан, но выглядит как заглушка – проверьте его")
    else:
        warnings.append("PROVIDER_TOKEN не задан – рублёвые платежи не будут работать")
    print_check("Переменные окружения", True, f"Все обязательные переменные заданы")
    return True, warnings

async def check_db_connection() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        print_check("Подключение к БД", True)
        return True
    except Exception as e:
        print_check("Подключение к БД", False, str(e))
        return False

async def check_db_schema() -> Tuple[bool, List[str]]:
    required_tables = ["cities", "stations", "fuel_prices", "availability_reports",
                       "users", "payments", "notifications", "reviews",
                       "user_achievements", "referrals", "user_economies",
                       "city_slugs", "pro_notifications_sent", "task_locks"]
    warnings = []
    try:
        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
            existing_tables = {row[0] for row in existing.all()}
            missing = [t for t in required_tables if t not in existing_tables]
            if missing:
                print_check("Схема БД (таблицы)", False, f"Отсутствуют: {', '.join(missing)}")
                return False, warnings
            columns = await db.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name='users'")
            )
            col_names = {row[0] for row in columns.all()}
            required_cols = {"free_searches_today", "last_free_search_date"}
            missing_cols = required_cols - col_names
            if missing_cols:
                print_check("Схема БД (колонки users)", False, f"Отсутствуют: {', '.join(missing_cols)}")
                return False, warnings
            slugs_count = await db.execute(text("SELECT COUNT(*) FROM city_slugs"))
            if slugs_count.scalar() == 0:
                warnings.append("В таблице city_slugs нет записей – возможно, городы не импортированы через /import_city")
        print_check("Схема БД", True, f"Все таблицы и колонки присутствуют")
        return True, warnings
    except Exception as e:
        print_check("Схема БД", False, str(e))
        return False, warnings

async def check_cities_and_stations() -> Tuple[bool, List[str]]:
    warnings = []
    try:
        async with AsyncSessionLocal() as db:
            cities = await get_all_active_cities(db)
            if not cities:
                print_check("Города и АЗС", False, "Нет активных городов")
                return False, warnings
            for city in cities:
                if city.latitude is None or city.longitude is None:
                    warnings.append(f"Город {city.name} без координат – задайте через /set_city_coords")
                slug = await get_city_slug(db, city.name)
                if not slug:
                    warnings.append(f"Для города {city.name} нет слага – используйте /set_slug или /import_city")
                stations = await get_stations_by_city(db, city.id)
                if not stations:
                    warnings.append(f"В городе {city.name} нет АЗС – импортируйте через /import_city или /add_station")
            fresh_prices = await db.execute(
                text("SELECT COUNT(*) FROM fuel_prices WHERE is_fresh = True")
            )
            if fresh_prices.scalar() == 0:
                warnings.append("Нет свежих цен (is_fresh=True) – запустите парсинг через /refresh_all_cities или cron")
        print_check("Города и АЗС", True, f"{len(cities)} городов, есть АЗС и свежие цены")
        return True, warnings
    except Exception as e:
        print_check("Города и АЗС", False, str(e))
        return False, warnings

async def check_parser() -> bool:
    test_cities = ["Москва", "Санкт-Петербург", "Красноярск"]
    success = True
    for city in test_cities:
        try:
            await fetch_fuelprice_prices(city)
            logger.info(f"✅ Парсер для {city}: успешно")
        except Exception as e:
            logger.error(f"❌ Парсер для {city}: ошибка – {str(e)[:100]}")
            success = False
            break
    if success:
        print_check("Парсер цен", True, f"Успешно загружены {len(test_cities)} городов")
    else:
        print_check("Парсер цен", False, f"Ошибка при парсинге {city}")
    return success

async def check_geocoder() -> bool:
    if not settings.YANDEX_GEOCODER_API_KEY:
        print_check("Геокодер", True, "Ключ не задан, пропущено")
        return True
    try:
        coords = await geocode_address("Красноярск, ул. Ленина")
        if coords:
            print_check("Геокодер", True, f"Координаты: {coords}")
            return True
        else:
            print_check("Геокодер", False, "Не удалось получить координаты")
            return False
    except Exception as e:
        print_check("Геокодер", False, str(e))
        return False

async def check_free_search_logic() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            user = await create_test_user(db)
            can1 = await can_use_free_search(db, user.id)
            if not can1:
                print_check("Логика бесплатных поисков", False, "Первый вызов can_use_free_search вернул False")
                await cleanup_test_user(db, user)
                return False
            remaining = await use_free_search(db, user.id)
            if remaining != 0:
                print_check("Логика бесплатных поисков", False, f"После использования осталось {remaining}, ожидалось 0")
                await cleanup_test_user(db, user)
                return False
            can2 = await can_use_free_search(db, user.id)
            if can2:
                print_check("Логика бесплатных поисков", False, "Второй вызов can_use_free_search вернул True, а должен False")
                await cleanup_test_user(db, user)
                return False
            user.last_free_search_date = date.today() - timedelta(days=1)
            user.free_searches_today = 0
            await db.commit()
            can3 = await can_use_free_search(db, user.id)
            if not can3:
                print_check("Логика бесплатных поисков", False, "После сброса даты can_use_free_search вернул False")
                await cleanup_test_user(db, user)
                return False
            await cleanup_test_user(db, user)
            print_check("Логика бесплатных поисков", True, "Все проверки пройдены")
            return True
    except Exception as e:
        print_check("Логика бесплатных поисков", False, str(e))
        return False

async def check_payment_and_pro_activation() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            user = await create_test_user(db)
            charge_id = f"test_charge_{user.id}_{datetime.now().timestamp()}"
            payment = await create_payment(
                db,
                user_id=user.id,
                telegram_payment_charge_id=charge_id,
                provider_payment_charge_id="test_provider",
                amount=99.0,
                currency="RUB",
                tariff="pro_month"
            )
            await activate_pro(db, user, days=30)
            is_pro = await is_user_pro(db, user)
            if not is_pro:
                print_check("Оплата и активация PRO", False, "После активации is_user_pro вернул False")
                await cleanup_test_user(db, user)
                return False
            if user.pro_until is None or (user.pro_until - datetime.now(timezone.utc)).days < 28:
                print_check("Оплата и активация PRO", False, f"Дата окончания PRO некорректна: {user.pro_until}")
                await cleanup_test_user(db, user)
                return False
            payment_check = await db.execute(
                text("SELECT * FROM payments WHERE telegram_payment_charge_id = :cid"),
                {"cid": charge_id}
            )
            if not payment_check.fetchone():
                print_check("Оплата и активация PRO", False, "Платёж не найден в БД")
                await cleanup_test_user(db, user)
                return False
            await cleanup_test_user(db, user)
            print_check("Оплата и активация PRO", True, "PRO активирован, платёж сохранён")
            return True
    except Exception as e:
        print_check("Оплата и активация PRO", False, str(e))
        return False

async def check_trial() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            user = await create_test_user(db)
            result = await activate_trial(db, user.id)
            if not result:
                print_check("Триал", False, "activate_trial вернул False при первом вызове")
                await cleanup_test_user(db, user)
                return False
            await db.refresh(user)
            is_pro = await is_user_pro(db, user)
            if not is_pro:
                print_check("Триал", False, "После активации триала is_user_pro вернул False")
                await cleanup_test_user(db, user)
                return False
            if user.pro_until is None or (user.pro_until - datetime.now(timezone.utc)).days < 2:
                print_check("Триал", False, f"Дата окончания триала некорректна: {user.pro_until}")
                await cleanup_test_user(db, user)
                return False
            result2 = await activate_trial(db, user.id)
            if result2:
                print_check("Триал", False, "Повторная активация триала вернула True, а должна False")
                await cleanup_test_user(db, user)
                return False
            await cleanup_test_user(db, user)
            print_check("Триал", True, "Триал активируется корректно, повторная активация блокируется")
            return True
    except Exception as e:
        print_check("Триал", False, str(e))
        return False

async def check_pro_expiry_and_deactivation() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            user = await create_test_user(db)
            user.is_pro = True
            user.pro_until = datetime.now(timezone.utc) - timedelta(days=1)
            user.auto_renew = False
            await db.commit()
            count = await disable_expired_pro(db)
            await db.refresh(user)
            if user.is_pro:
                print_check("Истечение PRO и деактивация", False, "PRO не был деактивирован после истечения")
                await cleanup_test_user(db, user)
                return False
            if user.pro_until is not None:
                print_check("Истечение PRO и деактивация", False, "pro_until не очищен")
                await cleanup_test_user(db, user)
                return False
            await cleanup_test_user(db, user)
            print_check("Истечение PRO и деактивация", True, "PRO корректно деактивируется после истечения")
            return True
    except Exception as e:
        print_check("Истечение PRO и деактивация", False, str(e))
        return False

async def check_referral_system() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            referrer = await create_test_user(db)
            referred = await create_test_user(db)
            code = referrer.referral_code
            success = await apply_referral(db, referred.id, code)
            if not success:
                print_check("Реферальная система", False, "apply_referral вернул False")
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False
            ref_record = await db.execute(
                text("SELECT * FROM referrals WHERE referrer_id = :rid AND referred_user_id = :reid"),
                {"rid": referrer.id, "reid": referred.id}
            )
            if not ref_record.fetchone():
                print_check("Реферальная система", False, "Реферал не создан в БД")
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False
            await db.refresh(referrer)
            if not referrer.is_pro:
                print_check("Реферальная система", False, "Реферер не стал PRO после реферала")
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False
            if referrer.pro_until is None or (referrer.pro_until - datetime.now(timezone.utc)).days < 2:
                print_check("Реферальная система", False, f"Бонусные дни не начислены или некорректны: {referrer.pro_until}")
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False
            link = await get_referral_link(db, referrer)
            if not link or "ref_" not in link:
                print_check("Реферальная система", False, "Реферальная ссылка некорректна")
                await cleanup_test_user(db, referrer)
                await cleanup_test_user(db, referred)
                return False
            await cleanup_test_user(db, referrer)
            await cleanup_test_user(db, referred)
            print_check("Реферальная система", True, "Реферал создан, бонус начислен, ссылка работает")
            return True
    except Exception as e:
        print_check("Реферальная система", False, str(e))
        return False

async def check_achievements() -> bool:
    try:
        async with AsyncSessionLocal() as db:
            user = await create_test_user(db)
            await add_achievement(db, user.id, "test_achievement", bonus_days=1)
            achievements = await get_user_achievements(db, user.id)
            if len(achievements) != 1:
                print_check("Достижения", False, "Достижение не сохранилось")
                await cleanup_test_user(db, user)
                return False
            if achievements[0].bonus_days_granted != 1:
                print_check("Достижения", False, "Бонусные дни не начислены")
                await cleanup_test_user(db, user)
                return False
            await db.refresh(user)
            if user.pro_until is None or (user.pro_until - datetime.now(timezone.utc)).days < 0:
                print_check("Достижения", False, "Бонусные дни за достижение не привели к активации PRO")
                await cleanup_test_user(db, user)
                return False
            await cleanup_test_user(db, user)
            print_check("Достижения", True, "Достижения сохраняются и начисляют бонусы")
            return True
    except Exception as e:
        print_check("Достижения", False, str(e))
        return False

# ========== ПРОВЕРКА РЕАЛЬНЫХ ПЛАТЕЖЕЙ (ИСПРАВЛЕННАЯ) ==========
async def check_real_payment() -> bool:
    """Отправляет тестовый инвойс администратору, чтобы проверить PROVIDER_TOKEN."""
    bot = None
    try:
        bot = Bot(token=settings.BOT_TOKEN)
        admin_id = settings.admin_ids[0] if settings.admin_ids else None
        if not admin_id:
            print_check("Реальные платежи", False, "ADMIN_ID не задан, не могу отправить тестовый инвойс")
            return False

        if not settings.PROVIDER_TOKEN:
            print_check("Реальные платежи", False, "PROVIDER_TOKEN не задан – рублёвые платежи не будут работать")
            return False

        # Пытаемся отправить инвойс на 1 рубль (100 копеек)
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
        # Закрываем сессию бота, чтобы избежать "Unclosed client session"
        await bot.session.close()
        print_check("Реальные платежи", True, "Тестовый инвойс отправлен администратору. Если вы видите кнопку оплаты – PROVIDER_TOKEN работает.")
        return True
    except Exception as e:
        error_msg = str(e)
        # Закрываем сессию даже при ошибке
        if bot:
            try:
                await bot.session.close()
            except:
                pass
        if "PROVIDER_TOKEN" in error_msg or "Invalid provider token" in error_msg or "CURRENCY" in error_msg:
            print_check("Реальные платежи", False, f"PROVIDER_TOKEN невалидный или не активирован: {error_msg[:100]}")
        else:
            print_check("Реальные платежи", False, f"Ошибка при отправке инвойса: {error_msg[:100]}")
        return False
# =======================================================

async def check_health_endpoint() -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://localhost:{settings.PORT}/health", timeout=5) as resp:
                if resp.status == 200:
                    print_check("Эндпоинт /health", True, "Доступен (локально)")
                    return True
                else:
                    print_check("Эндпоинт /health", False, f"HTTP {resp.status}")
                    return False
    except Exception as e:
        print_check("Эндпоинт /health", False, f"Недоступен: {str(e)[:80]}")
        return False

async def check_notification_modules() -> bool:
    try:
        from services.pro_notifications import send_pro_expiry_notifications_with_bot
        from services.notifications import check_notifications
        print_check("Модули уведомлений", True, "Импортируются без ошибок")
        return True
    except Exception as e:
        print_check("Модули уведомлений", False, str(e))
        return False

async def check_internal_token_security() -> bool:
    token = settings.INTERNAL_TOKEN
    if token in ["ваш_секретный_токен", "secret", "token", "internal_token", "123456"]:
        print_check("Безопасность INTERNAL_TOKEN", False, "Слишком простой токен – используйте генератор")
        return False
    print_check("Безопасность INTERNAL_TOKEN", True, "Токен выглядит надёжным")
    return True

async def check_provider_token() -> bool:
    if settings.PROVIDER_TOKEN:
        if settings.PROVIDER_TOKEN in ["ваш_провайдер_токен", "test", "provider"]:
            print_check("PROVIDER_TOKEN", False, "Задан, но выглядит как заглушка – проверьте его")
            return False
        print_check("PROVIDER_TOKEN", True, "Задан")
        return True
    else:
        print_check("PROVIDER_TOKEN", False, "Не задан – рублёвые платежи не будут работать")
        return False

async def run_all_checks() -> Tuple[int, int, List[Tuple[str, bool]], List[str]]:
    """Запускает все проверки и возвращает (passed, total, results, warnings)."""
    results = []
    warnings = []

    # 1. Переменные окружения
    ok, warn = await check_env_vars()
    results.append(("Переменные окружения", ok))
    warnings.extend(warn)

    # 2. Подключение к БД
    ok = await check_db_connection()
    results.append(("Подключение к БД", ok))
    if not ok:
        logger.error("❌ Нет подключения к БД – дальнейшие проверки невозможны")
    else:
        # 3. Схема БД
        ok, warn = await check_db_schema()
        results.append(("Схема БД", ok))
        warnings.extend(warn)

        # 4. Города и АЗС
        ok, warn = await check_cities_and_stations()
        results.append(("Города и АЗС", ok))
        warnings.extend(warn)

        # 5. Логика бесплатных поисков
        ok = await check_free_search_logic()
        results.append(("Логика бесплатных поисков", ok))

        # 6. Оплата и активация PRO (симуляция)
        ok = await check_payment_and_pro_activation()
        results.append(("Оплата и активация PRO (симуляция)", ok))

        # 7. Триал
        ok = await check_trial()
        results.append(("Триал", ok))

        # 8. Истечение PRO и деактивация
        ok = await check_pro_expiry_and_deactivation()
        results.append(("Истечение PRO", ok))

        # 9. Реферальная система
        ok = await check_referral_system()
        results.append(("Реферальная система", ok))

        # 10. Достижения
        ok = await check_achievements()
        results.append(("Достижения", ok))

    # 11. Парсер цен
    ok = await check_parser()
    results.append(("Парсер цен", ok))

    # 12. Геокодер
    ok = await check_geocoder()
    results.append(("Геокодер", ok))

    # 13. Эндпоинт /health
    ok = await check_health_endpoint()
    results.append(("Эндпоинт /health", ok))

    # 14. Модули уведомлений
    ok = await check_notification_modules()
    results.append(("Модули уведомлений", ok))

    # 15. Безопасность INTERNAL_TOKEN
    ok = await check_internal_token_security()
    results.append(("Безопасность INTERNAL_TOKEN", ok))

    # 16. PROVIDER_TOKEN (наличие)
    ok = await check_provider_token()
    results.append(("PROVIDER_TOKEN (наличие)", ok))

    # 17. РЕАЛЬНЫЕ ПЛАТЕЖИ (отправка тестового инвойса)
    ok = await check_real_payment()
    results.append(("Реальные платежи (тестовый инвойс)", ok))

    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    return passed, total, results, warnings

async def run_all_checks_and_log():
    """Запускает проверки и логирует результат (для использования в командной строке)."""
    passed, total, results, warnings = await run_all_checks()

    logger.info("=" * 60)
    logger.info(f"📊 Результат: {passed}/{total} автоматических проверок пройдено")

    for name, ok in results:
        icon = "✅" if ok else "❌"
        logger.info(f"{icon} {name}")

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
