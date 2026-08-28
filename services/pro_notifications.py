import logging
from datetime import datetime, timedelta, timezone
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from database.session import AsyncSessionLocal
from database.models import User
from database.crud import get_user, get_pro_notification_sent, mark_pro_notification_sent
from config import settings

logger = logging.getLogger(__name__)

async def send_pro_expiry_notifications_with_bot(bot: Bot):
    """Проверяет пользователей с активным PRO и отправляет уведомления о скором окончании, используя переданный экземпляр бота."""
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
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

            if hours <= 1 and hours > 0:
                if not await get_pro_notification_sent(db, user.id, '1h'):
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            f"⏰ Ваша PRO-подписка истекает через <b>1 час</b>!\n"
                            f"Продлите её сейчас, чтобы продолжать получать уведомления о ценах и экономить до 500 ₽ за заправку.\n"
                            f"Нажмите «💎 PRO» в меню, чтобы продлить.",
                            parse_mode="HTML"
                        )
                        await mark_pro_notification_sent(db, user.id, '1h')
                        logger.info(f"Отправлено уведомление 1h для {user.telegram_id}")
                    except TelegramBadRequest as e:
                        if "chat not found" in str(e) or "bot can't send messages" in str(e):
                            logger.warning(f"Пользователь {user.telegram_id} недоступен. Отключаем PRO.")
                            user.is_pro = False
                            user.pro_until = None
                            await db.commit()
                        else:
                            logger.error(f"Ошибка отправки уведомления пользователю {user.telegram_id}: {e}")
                continue

            if hours <= 3 and hours > 1:
                if not await get_pro_notification_sent(db, user.id, '3h'):
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            f"⏰ Ваша PRO-подписка истекает через <b>3 часа</b>!\n"
                            f"Не упустите возможность продлить и продолжать экономить.\n"
                            f"Нажмите «💎 PRO» в меню.",
                            parse_mode="HTML"
                        )
                        await mark_pro_notification_sent(db, user.id, '3h')
                        logger.info(f"Отправлено уведомление 3h для {user.telegram_id}")
                    except TelegramBadRequest as e:
                        if "chat not found" in str(e) or "bot can't send messages" in str(e):
                            logger.warning(f"Пользователь {user.telegram_id} недоступен. Отключаем PRO.")
                            user.is_pro = False
                            user.pro_until = None
                            await db.commit()
                        else:
                            logger.error(f"Ошибка отправки уведомления пользователю {user.telegram_id}: {e}")
                continue

            if days == 1:
                if not await get_pro_notification_sent(db, user.id, '1d'):
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            f"⏰ Ваша PRO-подписка истекает <b>завтра</b> (через 1 день)!\n"
                            f"Продлите сейчас, чтобы не потерять доступ к уведомлениям и графикам.\n"
                            f"Нажмите «💎 PRO» в меню.",
                            parse_mode="HTML"
                        )
                        await mark_pro_notification_sent(db, user.id, '1d')
                        logger.info(f"Отправлено уведомление 1d для {user.telegram_id}")
                    except TelegramBadRequest as e:
                        if "chat not found" in str(e) or "bot can't send messages" in str(e):
                            logger.warning(f"Пользователь {user.telegram_id} недоступен. Отключаем PRO.")
                            user.is_pro = False
                            user.pro_until = None
                            await db.commit()
                        else:
                            logger.error(f"Ошибка отправки уведомления пользователю {user.telegram_id}: {e}")
                continue

            if days == 2:
                if not await get_pro_notification_sent(db, user.id, '2d'):
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            f"⏰ Ваша PRO-подписка истекает через <b>2 дня</b>!\n"
                            f"Продлите заранее, чтобы не потерять доступ.\n"
                            f"Нажмите «💎 PRO» в меню.",
                            parse_mode="HTML"
                        )
                        await mark_pro_notification_sent(db, user.id, '2d')
                        logger.info(f"Отправлено уведомление 2d для {user.telegram_id}")
                    except TelegramBadRequest as e:
                        if "chat not found" in str(e) or "bot can't send messages" in str(e):
                            logger.warning(f"Пользователь {user.telegram_id} недоступен. Отключаем PRO.")
                            user.is_pro = False
                            user.pro_until = None
                            await db.commit()
                        else:
                            logger.error(f"Ошибка отправки уведомления пользователю {user.telegram_id}: {e}")
                continue

            if days == 3:
                if not await get_pro_notification_sent(db, user.id, '3d'):
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            f"⏰ Ваша PRO-подписка истекает через <b>3 дня</b>!\n"
                            f"Успейте продлить и продолжать экономить на топливе.\n"
                            f"Нажмите «💎 PRO» в меню.",
                            parse_mode="HTML"
                        )
                        await mark_pro_notification_sent(db, user.id, '3d')
                        logger.info(f"Отправлено уведомление 3d для {user.telegram_id}")
                    except TelegramBadRequest as e:
                        if "chat not found" in str(e) or "bot can't send messages" in str(e):
                            logger.warning(f"Пользователь {user.telegram_id} недоступен. Отключаем PRO.")
                            user.is_pro = False
                            user.pro_until = None
                            await db.commit()
                        else:
                            logger.error(f"Ошибка отправки уведомления пользователю {user.telegram_id}: {e}")
                continue
