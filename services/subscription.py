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
# 1. ФУНКЦИИ ПРОВЕРКИ И УПРАВЛЕНИЯ PRO-СТАТУСОМ
# ==========================================

async def is_user_pro(user_id: int) -> bool:
    """Проверяет, активна ли PRO-подписка у пользователя (по telegram_id или id)."""
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
            
            # Если флаг is_pro True и дата pro_until больше текущего момента
            if row.get("is_pro") and row.get("pro_until"):
                return row["pro_until"] > datetime.utcnow()
            return bool(row.get("is_pro", False))
    except Exception as e:
        logger.error(f"[Subscription] Ошибка проверки is_user_pro: {e}")
        return False

async def check_pro(user_id: int) -> bool:
    """
    Основной алиас проверки подписки для хендлеров (find.py, emergency.py, radar.py).
    Возвращает True, если PRO активно, иначе False.
    """
    return await is_user_pro(user_id)

async def get_user_pro_status(user_id: int) -> Dict[str, Any]:
    """Возвращает детальную информацию о статусе подписки."""
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
                return {"is_pro": False, "pro_until": None, "days_left": 0}
            
            is_pro = bool(row.get("is_pro", False))
            pro_until = row.get("pro_until")
            
            days_left = 0
            if pro_until:
                delta = pro_until - datetime.utcnow()
                days_left = max(0, delta.days)
                if delta.total_seconds() <= 0:
                    is_pro = False

            return {
                "is_pro": is_pro,
                "pro_until": pro_until,
                "days_left": days_left
            }
    except Exception as e:
        logger.error(f"[Subscription] Ошибка получения get_user_pro_status: {e}")
        return {"is_pro": False, "pro_until": None, "days_left": 0}

async def grant_pro(user_id: int, days: int = 30) -> bool:
    """Выдаёт или продлевает PRO-подписку пользователю на N дней."""
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
        logger.error(f"[Subscription] Ошибка при выдаче grant_pro: {e}")
        return False

async def revoke_pro(user_id: int) -> bool:
    """Отключает PRO-подписку у пользователя."""
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
# 2. ФОНОВЫЙ ВОРКЕР (ПРОВЕРКА И УВЕДОМЛЕНИЯ)
# ==========================================

async def check_expiring_subscriptions(bot: Bot):
    """Проверяет подписки, которые скоро закончатся или уже истекли."""
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
                        "Продлите подписку в меню /profile, чтобы не потерять доступ к радару цен и экстренному поиску.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            # Отключение истекших подписок
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
                        "Вы можете возобновить её в любой момент в разделе /profile.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"[SubscriptionService] Ошибка в check_expiring_subscriptions: {e}")

async def subscription_expiration_worker(bot: Bot):
    """Фоновый таск, проверяющий базу раз в 12 часов."""
    logger.info("[SubscriptionService] Воркер запущен.")
    while True:
        try:
            await check_expiring_subscriptions(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[SubscriptionService] Ошибка воркера: {e}")
        await asyncio.sleep(43200)
