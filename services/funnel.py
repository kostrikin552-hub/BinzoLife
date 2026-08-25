import logging
from datetime import datetime, timedelta, timezone
from aiogram import Bot
from database.session import AsyncSessionLocal
from database.crud import (
    get_funnel_users, advance_funnel_stage, get_users_without_first_search
)
from keyboards.reply import main_menu_keyboard
from config import settings

logger = logging.getLogger(__name__)
bot = Bot(token=settings.BOT_TOKEN)

FUNNEL_MESSAGES = {
    0: (
        "👋 Привет! Ты уже установил город и можешь искать заправки.\n"
        "Нажми «⛽ Найти заправку» и выбери топливо — я покажу лучшие варианты."
    ),
    1: (
        "👋 Ну как тебе BinzoLife? Удалось сэкономить?\n\n"
        "Если есть вопросы или пожелания — просто напиши мне. Я всегда на связи.\n\n"
        "А пока можешь поискать заправку ещё раз — я покажу лучшие варианты."
    ),
    2: (
        "📊 Вчера 1 200 водителей в твоём городе нашли топливо без очередей.\n\n"
        "Ты ещё не пользовался ботом сегодня? Нажми «Найти заправку» — я покажу актуальные цены."
    ),
    3: (
        "⚠️ Ты уже 3 раза искал заправку. Бесплатная выдача работает, но данные устаревают каждые 2 часа.\n\n"
        "🔥 **Подключи PRO за 99 ₽** — и ты всегда будешь знать о появлении топлива и снижении цен.\n"
        "Это окупится с первой заправки.\n\n"
        "💰 **Ты теряешь до 500 ₽ на каждой заправке без уведомлений.**\n"
        "Узнай, сколько ты уже потерял — нажми «💎 PRO» в меню."
    ),
    4: (
        "🎁 Специально для тебя: напиши отзыв о боте (можно просто текст), и я начислю тебе **3 дня PRO бесплатно**!\n\n"
        "Просто нажми «⭐ Оставить отзыв» в меню.\n\n"
        "Твой отзыв помогает нам становиться лучше, а ты получаешь бонус."
    ),
    5: (
        "Мы скучаем! Ты мог бы уже сэкономить до 2 000 ₽ на топливе, если бы использовал PRO.\n\n"
        "Вернись и попробуй ещё раз. Нажми «Найти заправку» — я покажу, что изменилось.\n\n"
        "**Последний шанс:** оформи PRO со скидкой 50% на первый месяц — промокод **SAVE50** (действует до конца недели).\n\n"
        "До встречи в боте!"
    )
}

async def send_funnel_message(user_id: int, text: str):
    try:
        await bot.send_message(user_id, text, reply_markup=main_menu_keyboard())
        logger.info(f"Воронка: отправлено сообщение стадии пользователю {user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки воронки пользователю {user_id}: {e}")

async def process_funnel():
    """Основная функция, вызываемая из фоновой задачи"""
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)

        # Стадия 0
        users_stage0 = await get_users_without_first_search(db)
        for user in users_stage0:
            if user.created_at and (now - user.created_at) > timedelta(hours=1):
                if user.funnel_stage == 0 and (user.last_funnel_message_at is None or (now - user.last_funnel_message_at) > timedelta(hours=24)):
                    await send_funnel_message(user.telegram_id, FUNNEL_MESSAGES[0])
                    await advance_funnel_stage(db, user.id, 1)

        # Стадия 1
        users_stage1 = await get_funnel_users(db, stage=1, days_after=1)
        for user in users_stage1:
            await send_funnel_message(user.telegram_id, FUNNEL_MESSAGES[1])
            await advance_funnel_stage(db, user.id, 2)

        # Стадия 2
        users_stage2 = await get_funnel_users(db, stage=2, days_after=3)
        for user in users_stage2:
            await send_funnel_message(user.telegram_id, FUNNEL_MESSAGES[2])
            await advance_funnel_stage(db, user.id, 3)

        # Стадия 3
        users_stage3 = await get_funnel_users(db, stage=3, days_after=7)
        for user in users_stage3:
            if user.is_pro:
                await advance_funnel_stage(db, user.id, 5, message_sent=False)
                continue
            await send_funnel_message(user.telegram_id, FUNNEL_MESSAGES[3])
            await advance_funnel_stage(db, user.id, 4)

        # Стадия 4
        users_stage4 = await get_funnel_users(db, stage=4, days_after=14)
        for user in users_stage4:
            if user.is_pro:
                await advance_funnel_stage(db, user.id, 5, message_sent=False)
                continue
            await send_funnel_message(user.telegram_id, FUNNEL_MESSAGES[4])
            await advance_funnel_stage(db, user.id, 5)
