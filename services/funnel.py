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
        "👋 Вы искали АЗС, но не заправились. \n"
        "Цены на топливо меняются каждый час. Возможно, сейчас цена стала ещё ниже.\n\n"
        "Проверьте за 10 секунд: нажмите «Найти заправку» и убедитесь сами."
    ),
    1: (
        "📊 За вчера вы могли сэкономить до 50 ₽, если бы заправились по самой низкой цене.\n"
        "За месяц это уже 1 500 ₽ — неплохая экономия, правда?\n\n"
        "С PRO вы будете получать уведомления о снижении цены и никогда не пропустите выгодное предложение.\n"
        "Попробуйте 3 дня бесплатно — активируйте в меню PRO."
    ),
    2: (
        "🔥 У нас новые данные: в вашем городе цена на топливо упала на 2 АЗС. \n"
        "Вы уже проверили?\n\n"
        "Кстати, вы использовали только несколько бесплатных поисков. \n"
        "С PRO поиск безлимитный, и вы всегда будете знать, где самая низкая цена.\n\n"
        "Станьте PRO сегодня и начните экономить прямо сейчас."
    ),
    3: (
        "⏳ Ваш бесплатный пробный период (если был активирован) заканчивается.\n"
        "Или вы ещё не пользовались триалом? \n"
        "С PRO вы получите уведомления о снижении цены и сможете экономить до 500 ₽ за заправку.\n\n"
        "Не упустите возможность — оформите подписку сейчас, пока цена 99 ₽."
    )
}

async def send_funnel_message(user_id: int, text: str):
    try:
        await bot.send_message(user_id, text, reply_markup=main_menu_keyboard())
        logger.info(f"Воронка: отправлено сообщение стадии пользователю {user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки воронки пользователю {user_id}: {e}")

async def process_funnel():
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)

        # Стадия 0 – через 1 час после первого поиска
        users_stage0 = await get_funnel_users(db, stage=0, days_after=0)
        for user in users_stage0:
            if user.first_search_at and (now - user.first_search_at) > timedelta(hours=1):
                if user.last_funnel_message_at is None or (now - user.last_funnel_message_at) > timedelta(hours=24):
                    await send_funnel_message(user.telegram_id, FUNNEL_MESSAGES[0])
                    await advance_funnel_stage(db, user.id, 1)

        # Стадия 1 – через 1 день
        users_stage1 = await get_funnel_users(db, stage=1, days_after=1)
        for user in users_stage1:
            if user.is_pro:
                await advance_funnel_stage(db, user.id, 4, message_sent=False)
                continue
            await send_funnel_message(user.telegram_id, FUNNEL_MESSAGES[1])
            await advance_funnel_stage(db, user.id, 2)

        # Стадия 2 – через 3 дня
        users_stage2 = await get_funnel_users(db, stage=2, days_after=3)
        for user in users_stage2:
            if user.is_pro:
                await advance_funnel_stage(db, user.id, 4, message_sent=False)
                continue
            await send_funnel_message(user.telegram_id, FUNNEL_MESSAGES[2])
            await advance_funnel_stage(db, user.id, 3)

        # Стадия 3 – через 7 дней
        users_stage3 = await get_funnel_users(db, stage=3, days_after=7)
        for user in users_stage3:
            if user.is_pro:
                await advance_funnel_stage(db, user.id, 4, message_sent=False)
                continue
            await send_funnel_message(user.telegram_id, FUNNEL_MESSAGES[3])
            await advance_funnel_stage(db, user.id, 4)  # завершено
