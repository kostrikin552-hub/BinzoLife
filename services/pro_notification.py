import logging
from datetime import datetime, timedelta, timezone
from aiogram import Bot
from sqlalchemy import select
from database.session import AsyncSessionLocal
from database.models import User
from database.crud import get_user, get_pro_notification_sent, mark_pro_notification_sent
from config import settings

logger = logging.getLogger(__name__)
bot = Bot(token=settings.BOT_TOKEN)

async def send_pro_expiry_notifications():
    """Проверяет пользователей с активным PRO и отправляет уведомления о скором окончании"""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        # Все пользователи с активным PRO (is_pro=True и pro_until > now)
        users = await db.execute(
            select(User).where(
                User.is_pro == True,
                User.pro_until > now
            )
        )
        users = users.scalars().all()

        for user in users:
            if not user.pro_until:
                continue

            remaining = user.pro_until - now
            days = remaining.days
            seconds = remaining.total_seconds()
            hours = seconds / 3600

            # Определяем, какое уведомление отправить
            # Приоритет: сначала проверяем самые близкие к окончанию
            # За 1 час
            if hours <= 1 and hours > 0:
                if not await get_pro_notification_sent(db, user.id, '1h'):
                    await bot.send_message(
                        user.telegram_id,
                        f"⏰ Ваша PRO-подписка истекает через **1 час**!\n"
                        f"Продлите её сейчас, чтобы продолжать получать уведомления о ценах и экономить до 500 ₽ за заправку.\n"
                        f"Нажмите «💎 PRO» в меню, чтобы продлить."
                    )
                    await mark_pro_notification_sent(db, user.id, '1h')
                    logger.info(f"Отправлено уведомление 1h для {user.telegram_id}")
                continue  # после отправки самого срочного, выходим, чтобы не слать другие

            # За 3 часа
            if hours <= 3 and hours > 1:
                if not await get_pro_notification_sent(db, user.id, '3h'):
                    await bot.send_message(
                        user.telegram_id,
                        f"⏰ Ваша PRO-подписка истекает через **3 часа**!\n"
                        f"Не упустите возможность продлить и продолжать экономить.\n"
                        f"Нажмите «💎 PRO» в меню."
                    )
                    await mark_pro_notification_sent(db, user.id, '3h')
                    logger.info(f"Отправлено уведомление 3h для {user.telegram_id}")
                continue

            # За 1 день
            if days == 1:
                if not await get_pro_notification_sent(db, user.id, '1d'):
                    await bot.send_message(
                        user.telegram_id,
                        f"⏰ Ваша PRO-подписка истекает **завтра** (через 1 день)!\n"
                        f"Продлите сейчас, чтобы не потерять доступ к уведомлениям и графикам.\n"
                        f"Нажмите «💎 PRO» в меню."
                    )
                    await mark_pro_notification_sent(db, user.id, '1d')
                    logger.info(f"Отправлено уведомление 1d для {user.telegram_id}")
                continue

            # За 2 дня
            if days == 2:
                if not await get_pro_notification_sent(db, user.id, '2d'):
                    await bot.send_message(
                        user.telegram_id,
                        f"⏰ Ваша PRO-подписка истекает через **2 дня**!\n"
                        f"Продлите заранее, чтобы не потерять доступ.\n"
                        f"Нажмите «💎 PRO» в меню."
                    )
                    await mark_pro_notification_sent(db, user.id, '2d')
                    logger.info(f"Отправлено уведомление 2d для {user.telegram_id}")
                continue

            # За 3 дня
            if days == 3:
                if not await get_pro_notification_sent(db, user.id, '3d'):
                    await bot.send_message(
                        user.telegram_id,
                        f"⏰ Ваша PRO-подписка истекает через **3 дня**!\n"
                        f"Успейте продлить и продолжать экономить на топливе.\n"
                        f"Нажмите «💎 PRO» в меню."
                    )
                    await mark_pro_notification_sent(db, user.id, '3d')
                    logger.info(f"Отправлено уведомление 3d для {user.telegram_id}")
                continue
