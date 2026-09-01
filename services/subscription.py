# services/subscription.py
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from aiogram import Bot
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

async def check_expiring_subscriptions(bot: Bot):
    """Проверяет подписки, истекающие через 3 дня, 1 день или уже истекшие."""
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
                        u["telegram_id"],
                        "⏳ <b>Ваша PRO-подписка истекает через 3 дня!</b>\n\n"
                        "Продлите подписку заранее в меню /profile, чтобы не потерять доступ к радару цен и экстренному поиску.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            # 2. Отключение истекших подписок
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
        logger.error(f"[SubscriptionService] Ошибка проверки подписок: {e}")

async def subscription_expiration_worker(bot: Bot):
    """Фоновый воркер для ежедневной проверки подписок."""
    logger.info("[SubscriptionService] Воркер запущен.")
    while True:
        try:
            await check_expiring_subscriptions(bot)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[SubscriptionService] Необработанная ошибка воркера: {e}")
        # Проверка каждые 12 часов
        await asyncio.sleep(43200)
