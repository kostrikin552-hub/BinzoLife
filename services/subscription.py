# services/subscription.py — ПОЛНАЯ ИСПРАВЛЕННАЯ ВЕРСИЯ
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Union, Dict, Any
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# =====================================================================
# 1. ФУНКЦИИ ФОРМАТИРОВАНИЯ
# =====================================================================

def format_pro_until(pro_until: Optional[Union[datetime, str]]) -> str:
    if not pro_until:
        return "❌ Не активна"
    if isinstance(pro_until, str):
        try:
            pro_until = datetime.fromisoformat(pro_until.replace("Z", "+00:00"))
        except Exception:
            return str(pro_until)
    if pro_until.tzinfo is not None:
        pro_until = pro_until.astimezone(timezone.utc).replace(tzinfo=None)
    now = datetime.utcnow()
    if pro_until <= now:
        return "❌ Истекла"
    delta = pro_until - now
    days_left = delta.days
    hours_left = delta.seconds // 3600
    date_str = pro_until.strftime("%d.%m.%Y")
    if days_left > 0:
        return f"до {date_str} (осталось {days_left} дн.)"
    else:
        return f"до {date_str} (осталось {hours_left} ч.)"

def get_pro_status_text(is_pro: bool, pro_until: Optional[datetime]) -> str:
    if is_pro and pro_until:
        dt_val = pro_until
        if dt_val.tzinfo is not None:
            dt_val = dt_val.astimezone(timezone.utc).replace(tzinfo=None)
        if dt_val > datetime.utcnow():
            return f"🌟 <b>PRO-аккаунт</b> ({format_pro_until(pro_until)})"
    return "Стандартный (Базовый доступ)"

def format_subscription_info(user_data: Dict[str, Any]) -> str:
    is_pro = bool(user_data.get("is_pro", False))
    pro_until = user_data.get("pro_until")
    return get_pro_status_text(is_pro, pro_until)

# =====================================================================
# 2. ПРОВЕРКА СТАТУСА ПОДПИСКИ (ИСПРАВЛЕНА)
# =====================================================================

async def is_user_pro(user_id: int) -> bool:
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
                logger.debug(f"[Subscription] is_user_pro: пользователь {user_id} не найден")
                return False
            if not row.get("is_pro", False):
                logger.debug(f"[Subscription] is_user_pro: пользователь {user_id} не PRO")
                return False
            pro_until = row.get("pro_until")
            if pro_until is None:
                logger.debug(f"[Subscription] is_user_pro: пользователь {user_id} имеет бессрочный PRO")
                return True
            if pro_until.tzinfo is not None:
                pro_until = pro_until.astimezone(timezone.utc).replace(tzinfo=None)
            now = datetime.utcnow()
            is_active = pro_until > now
            logger.debug(f"[Subscription] is_user_pro: user_id={user_id}, pro_until={pro_until}, now={now}, active={is_active}")
            return is_active
    except Exception as e:
        logger.error(f"[Subscription] Ошибка проверки is_user_pro для {user_id}: {e}")
        return False

async def check_pro(user_id: int) -> bool:
    return await is_user_pro(user_id)

async def get_user_pro_status(user_id: int) -> Dict[str, Any]:
    try:
        async with AsyncSessionLocal() as db:
            stmt = text("""
                SELECT id, telegram_id, is_pro, pro_until, created_at 
                FROM users 
                WHERE telegram_id = :uid OR id = :uid
                LIMIT 1;
            """)
            row = (await db.execute(stmt, {"uid": user_id})).mappings().first()
            if not row:
                return {"is_pro": False, "pro_until": None, "days_left": 0, "formatted": "❌ Не активна"}
            is_pro = bool(row.get("is_pro", False))
            pro_until = row.get("pro_until")
            days_left = 0
            if pro_until:
                if pro_until.tzinfo is not None:
                    pro_until_utc = pro_until.astimezone(timezone.utc).replace(tzinfo=None)
                else:
                    pro_until_utc = pro_until
                delta = pro_until_utc - datetime.utcnow()
                days_left = max(0, delta.days)
                if delta.total_seconds() <= 0:
                    is_pro = False
            return {
                "is_pro": is_pro,
                "pro_until": row.get("pro_until"),
                "days_left": days_left,
                "formatted": format_pro_until(row.get("pro_until"))
            }
    except Exception as e:
        logger.error(f"[Subscription] Ошибка получения get_user_pro_status: {e}")
        return {"is_pro": False, "pro_until": None, "days_left": 0, "formatted": "Ошибка загрузки"}

# =====================================================================
# 3. НАЧИСЛЕНИЕ, ПРОДЛЕНИЕ И ОТЗЫВ ПОДПИСОК
# =====================================================================

async def grant_pro(user_id: int, days: int = 30) -> bool:
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
        logger.error(f"[Subscription] Ошибка в grant_pro для {user_id}: {e}")
        return False

async def extend_user_pro(user_id: int, days: int = 30) -> bool:
    return await grant_pro(user_id, days)

async def revoke_pro(user_id: int) -> bool:
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
        logger.error(f"[Subscription] Ошибка в revoke_pro для {user_id}: {e}")
        return False

# =====================================================================
# 4. ФОНОВЫЙ ВОРКЕР ПРОВЕРКИ (С ОБРАБОТКОЙ TelegramForbiddenError)
# =====================================================================

async def check_expiring_subscriptions(bot: Bot):
    try:
        async with AsyncSessionLocal() as db:
            # 1. Предупреждение за 3 дня
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
                        chat_id=u["telegram_id"],
                        text=(
                            "⏳ <b>Ваша PRO-подписка истекает через 3 дня!</b>\n\n"
                            "Продлите подписку в меню /profile, чтобы не потерять доступ."
                        ),
                        parse_mode="HTML"
                    )
                    await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    # Пользователь заблокировал бота
                    await db.execute(text("UPDATE users SET is_active = false WHERE telegram_id = :uid"), {"uid": u["telegram_id"]})
                    await db.commit()
                except Exception:
                    pass

            # 2. Предупреждение за 1 день
            stmt_1d = text("""
                SELECT id, telegram_id, pro_until 
                FROM users 
                WHERE is_pro = true 
                  AND pro_until BETWEEN NOW() AND NOW() + INTERVAL '1 day'
                  AND pro_until > NOW() + INTERVAL '23 hours';
            """)
            exp_1d = (await db.execute(stmt_1d)).mappings().all()
            for u in exp_1d:
                try:
                    await bot.send_message(
                        chat_id=u["telegram_id"],
                        text=(
                            "⚠️ <b>Внимание: PRO-подписка заканчивается завтра!</b>\n\n"
                            "Продлите подписку в разделе /profile."
                        ),
                        parse_mode="HTML"
                    )
                    await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    await db.execute(text("UPDATE users SET is_active = false WHERE telegram_id = :uid"), {"uid": u["telegram_id"]})
                    await db.commit()
                except Exception:
                    pass

            # 3. Отключение истекших подписок
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
                        chat_id=u["telegram_id"],
                        text=(
                            "❌ <b>Срок действия вашей PRO-подписки завершён.</b>\n\n"
                            "Возобновить доступ можно через команду /profile."
                        ),
                        parse_mode="HTML"
                    )
                    await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    await db.execute(text("UPDATE users SET is_active = false WHERE telegram_id = :uid"), {"uid": u["telegram_id"]})
                    await db.commit()
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"[SubscriptionService] Ошибка в check_expiring_subscriptions: {e}")

async def subscription_expiration_worker(bot: Bot):
    logger.info("[SubscriptionService] Воркер контроля подписок запущен.")
    await asyncio.sleep(30)
    while True:
        try:
            await check_expiring_subscriptions(bot)
        except asyncio.CancelledError:
            logger.info("[SubscriptionService] Воркер контроля подписок остановлен.")
            break
        except Exception as e:
            logger.error(f"[SubscriptionService] Необработанная ошибка воркера: {e}")
        await asyncio.sleep(43200)

subscription_worker = subscription_expiration_worker
run_subscription_worker = subscription_expiration_worker
