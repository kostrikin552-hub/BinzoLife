# services/pro_notifications.py – ИСПРАВЛЕННЫЙ (добавлена проверка на ID бота)

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
bot = Bot(token=settings.BOT_TOKEN)

# Получаем ID бота один раз при первом вызове
_BOT_ID = None

async def get_bot_id():
    global _BOT_ID
    if _BOT_ID is None:
        me = await bot.get_me()
        _BOT_ID = me.id
    return _BOT_ID

async def send_pro_expiry_notifications():
    """Проверяет пользователей с активным PRO и отправляет уведомления о скором окончании"""
    now = datetime.now(timezone.utc)
    bot_id = await get_bot_id()
    async with AsyncSessionLocal() as db:
        # Все пользователи с активным PRO (is_pro=True и pro_until > now), исключаем самого бота
        users = await db.execute(
            select(User).where(
                User.is_pro == True,
                User.pro_until > now,
                User.telegram_id != bot_id  # <-- Исключаем самого бота
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
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            f"⏰ Ваша PRO-подписка истекает через **1 час**!\n"
                            f"Продлите её сейчас, чтобы продолжать получать уведомления о ценах и экономить до 500 ₽ за заправку.\n"
                            f"Нажмите «💎 PRO» в меню, чтобы продлить."
                        )
                        await mark_pro_notification_sent(db, user.id, '1h')
                        logger.info(f"Отправлено уведомление 1h для {user.telegram_id}")
                    except TelegramBadRequest as e:
                        if "chat not found" in str(e) or "bot can't send messages" in str(e):
                            logger.warning(f"Пользователь {user.telegram_id} недоступен (заблокировал бота или это бот). Отключаем PRO.")
                            # Отключаем PRO пользователю
                            user.is_pro = False
                            user.pro_until = None
                            await db.commit()
                        else:
                            logger.error(f"Ошибка отправки уведомления пользователю {user.telegram_id}: {e}")
                continue

            # За 3 часа
            if hours <= 3 and hours > 1:
                if not await get_pro_notification_sent(db, user.id, '3h'):
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            f"⏰ Ваша PRO-подписка истекает через **3 часа**!\n"
                            f"Не упустите возможность продлить и продолжать экономить.\n"
                            f"Нажмите «💎 PRO» в меню."
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

            # За 1 день
            if days == 1:
                if not await get_pro_notification_sent(db, user.id, '1d'):
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            f"⏰ Ваша PRO-подписка истекает **завтра** (через 1 день)!\n"
                            f"Продлите сейчас, чтобы не потерять доступ к уведомлениям и графикам.\n"
                            f"Нажмите «💎 PRO» в меню."
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

            # За 2 дня
            if days == 2:
                if not await get_pro_notification_sent(db, user.id, '2d'):
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            f"⏰ Ваша PRO-подписка истекает через **2 дня**!\n"
                            f"Продлите заранее, чтобы не потерять доступ.\n"
                            f"Нажмите «💎 PRO» в меню."
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

            # За 3 дня
            if days == 3:
                if not await get_pro_notification_sent(db, user.id, '3d'):
                    try:
                        await bot.send_message(
                            user.telegram_id,
                            f"⏰ Ваша PRO-подписка истекает через **3 дня**!\n"
                            f"Успейте продлить и продолжать экономить на топливе.\n"
                            f"Нажмите «💎 PRO» в меню."
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
