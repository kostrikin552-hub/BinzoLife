# services/subscription.py
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Union, Dict, Any
from aiogram import Bot
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# ==========================================
# 1. ФУНКЦИИ ФОРМАТИРОВАНИЯ И ОТОБРАЖЕНИЯ
# ==========================================

def format_pro_until(pro_until: Optional[Union[datetime, str]]) -> str:
    """
    Форматирует дату окончания подписки в понятный для пользователя вид:
    'до 15.10.2026 (осталось 12 дн.)' или 'Не активна'.
    """
    if not pro_until:
        return "❌ Не активна"

    if isinstance(pro_until, str):
        try:
            pro_until = datetime.fromisoformat(pro_until.replace("Z", "+00:00"))
        except Exception:
            return str(pro_until)

    # Если datetime с таймзоной, приводим к naive для сравнения
    dt_val = pro_until.replace(tzinfo=None) if hasattr(pro_until, "tzinfo") and pro_until.tzinfo else pro_until
    now = datetime.utcnow()

    if dt_val <= now:
        return "❌ Истекла"

    delta = dt_val - now
    days_left = delta.days
    hours_left = delta.seconds // 3600

    date_str = dt_val.strftime("%d.%m.%Y")
    if days_left > 0:
        return f"до {date_str} (осталось {days_left} дн.)"
    else:
        return f"до {date_str} (осталось {hours_left} ч.)"


def get_pro_status_text(is_pro: bool, pro_until: Optional[datetime]) -> str:
    """Генерирует готовый текстовый бейдж для профиля."""
    if is_pro and pro_until and (pro_until.replace(tzinfo=None) if pro_until.tzinfo else pro_until) > datetime.utcnow():
        return f"🌟 <b>PRO-аккаунт</b> ({format_pro_until(pro_until)})"
    return "Стандартный (Базовый доступ)"


def format_subscription_info(user_data: Dict[str, Any]) -> str:
    """Форматирует сводку подписки для сообщения профиля."""
    is_pro = bool(user_data.get("is_pro", False))
    pro_until = user_data.get("pro_until")
    return get_pro_status_text(is_pro, pro_until)


# ==========================================
# 2. ПРОВЕРКА СТАТУСА И ДОСТУПА В БД
# ==========================================

async def is_user_pro(user_id: int) -> bool:
    """Проверяет, активен ли PRO-статус у пользователя."""
    try:
        async with AsyncSessionLocal() as db:
            stmt = text("""
                SELECT is_pro, pro_until 
                FROM users 
                WHERE telegram_id = :uid OR id = :uid
                LIMIT 1;
            """)
            row = (await db.execute(stmt, {"uid": user_id})).mappings().first()
            if not row:
                return False

            if row.get("is_pro") and row.get("pro_until"):
                dt = row["pro_until"]
                dt_val = dt.replace(tzinfo=None) if dt.tzinfo else dt
                return dt_val > datetime.utcnow()
            return bool(row.get("is_pro", False))
    except Exception as e:
        logger.error(f"[Subscription] Ошибка is_user_pro: {e}")
        return False


async def check_pro(user_id: int) -> bool:
    """Основной алиас для хендлеров."""
    return await is_user_pro(user_id)


async def get_user_pro_status(user_id: int) -> Dict[str, Any]:
    """Возвращает структурированный статус подписки."""
    try:
        async with AsyncSessionLocal() as db:
            stmt = text("""
                SELECT id, telegram_id, is_pro, pro_until 
                FROM users 
                WHERE telegram_id = :uid OR id = :uid
                LIMIT 1;
            """)
            row = (await db.execute(stmt, {"uid": user_id})).mappings().first()
            if not row:
                return {"is_pro": False, "pro_until": None, "days_left": 0, "formatted": "Не активна"}

            is_pro = bool(row.get("is_pro", False))
            pro_until = row.get("pro_until")
            days_left = 0

            if pro_until:
                dt_val = pro_until.replace(tzinfo=None) if pro_until.tzinfo else pro_until
                delta = dt_val - datetime.utcnow()
                days_left = max(0, delta.days)
                if delta.total_seconds() <= 0:
                    is_pro = False

            return {
                "is_pro": is_pro,
                "pro_until": pro_until,
                "days_left": days_left,
                "formatted": format_pro_until(pro_until)
            }
    except Exception as e:
        logger.error(f"[Subscription] Ошибка get_user_pro_status: {e}")
        return {"is_pro": False, "pro_until": None, "days_left": 0, "formatted": "Ошибка"}


# ==========================================
# 3. НАЧИСЛЕНИЕ И ПРОДЛЕНИЕ ПОДПИСКИ
# ==========================================

async def grant_pro(user_id: int, days: int = 30) -> bool:
    """Выдает или продлевает подписку на заданное число дней."""
    try:
        async with AsyncSessionLocal() as db:
            stmt = text("""
                UPDATE users 
                SET is_pro = true,
                    pro_until = CASE 
                        WHEN pro_until IS NOT NULL AND pro_until > NOW() 
                        THEN pro_until + (:days || ' days')::interval
                        ELSE NOW() + (:days || ' days')::interval
                    END
                WHERE telegram_id = :uid OR id = :uid
                RETURNING id;
            """)
            res = await db.execute(stmt, {"uid": user_id, "days": days})
            await db.commit()
            return bool(res.rowcount > 0)
    except Exception as e:
        logger.error(f"[Subscription] Ошибка grant_pro: {e}")
        return False


async def extend_user_pro(user_id: int, days: int = 30) -> bool:
    """Алиас для хендлеров оплаты."""
    return await grant_pro(user_id, days)


async def revoke_pro(user_id: int) -> bool:
    """Отзывает подписку."""
    try:
        async with AsyncSessionLocal() as db:
            stmt = text("""
                UPDATE users 
                SET is_pro = false, pro_until = NULL 
                WHERE telegram_id = :uid OR id = :uid;
            """)
            await db.execute(stmt, {"uid": user_id})
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"[Subscription] Ошибка revoke_pro: {e}")
        return False


# ==========================================
# 4. ФОНОВЫЙ ВОРКЕР ПРОВЕРКИ
# ==========================================

async def check_expiring_subscriptions(bot: Bot):
    """Оповещает об истекающих подписках и отключает завершенные."""
    try:
        async with AsyncSessionLocal() as db:
            # Предупреждение за 3 дня
            stmt_3d = text("""
                SELECT id, telegram_id, pro_until 
                FROM users 
                WHERE is_pro = true 
                  AND pro_until BETWEEN NOW() AND NOW() + INTERVAL '3 days'
                  AND pro_until > NOW() + INTERVAL '2 days 23 hours';
            """)
            exp_3d = (await db.execute(stmt_3d)).mappings().all()
            for u in exp_3d:
                try:
                    await bot.send_message(
                        u["telegram_id"],
                        "⏳ <b>Ваша PRO-подписка истекает через 3 дня!</b>\n\n"
                        "Продлите её в меню /profile, чтобы не потерять доступ к экстренному поиску и мониторингу очередей.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            # Отключение истекших
            stmt_expired = text("""
                UPDATE users 
                SET is_pro = false 
                WHERE is_pro = true AND pro_until < NOW()
                RETURNING telegram_id;
            """)
            expired = (await db.execute(stmt_expired)).mappings().all()
            await db.commit()

            for u in expired:
                try:
                    await bot.send_message(
                        u["telegram_id"],
                        "❌ <b>Срок действия вашей PRO-подписки завершён.</b>\n\n"
                        "Вы можете возобновить подписку в любой момент командой /profile.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"[SubscriptionService] Ошибка воркера: {e}")


async def subscription_expiration_worker(bot: Bot):
    """Воркер, запускаемый в main.py."""
    logger.info("[SubscriptionService] Воркер запущен.")
    while True:
        try:
            await check_expiring_subscriptions(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[SubscriptionService] Исключение воркера: {e}")
        await asyncio.sleep(43200)
