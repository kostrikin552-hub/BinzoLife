# handlers/contest.py — ПОЛНАЯ ВЕРСИЯ
import asyncio
import logging
import random
import html
from aiogram import Router, types, F
from aiogram.filters import Command
from sqlalchemy import select
from config import settings
from database.session import AsyncSessionLocal
from database.models import User

router = Router()
logger = logging.getLogger(__name__)

def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS

@router.message(Command("contest"))
async def start_contest(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Нет прав.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "Использование: /contest Текст конкурса\n\n"
            "Например: /contest Угадайте цену на завтра! Ответы принимаются до 23:59."
        )
        return
    contest_text = parts[1]

    async with AsyncSessionLocal() as db:
        users = await db.execute(select(User))
        users = users.scalars().all()
        sent = 0
        for user in users:
            try:
                await message.bot.send_message(
                    user.telegram_id,
                    f"🎯 КОНКУРС!\n\n{contest_text}\n\n"
                    "Ответы присылайте в личные сообщения этому боту.\n"
                    "Победитель получит PRO-подписку на месяц!"
                )
                sent += 1
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"Не удалось отправить конкурс пользователю {user.telegram_id}: {e}")
    await message.answer(f"✅ Конкурс отправлен {sent} пользователям.")

async def pick_and_announce_winner(bot, chat_id: int, participants: list):
    """Безопасный выбор победителя с защитой от пустого списка и @None."""
    if not participants:
        await bot.send_message(chat_id, "⚠️ В розыгрыше нет участников.")
        return
    winner = random.choice(participants)
    if getattr(winner, "username", None):
        user_mention = f"@{html.escape(winner.username)}"
    else:
        full_name = html.escape(getattr(winner, "first_name", "Победитель") or "Участник")
        user_mention = f'<a href="tg://user?id={winner.telegram_id}">{full_name}</a>'
    message_text = (
        f"🎉 <b>Итоги розыгрыша подведены!</b>\n\n"
        f"Поздравляем победителя: {user_mention}!\n"
        f"🎁 Приз: 50 литров топлива или 1 месяц PRO-подписки.\n\n"
        f"Мы свяжемся с вами для передачи приза."
    )
    await bot.send_message(chat_id, message_text, parse_mode="HTML")
