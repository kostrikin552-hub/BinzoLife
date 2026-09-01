import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from aiogram import Bot
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

# =====================================================================
# 1. ЛОГИКА НАПОМИНАНИЙ И ОНБОРДИНГА PRO
# =====================================================================

async def send_pro_onboarding_reminders(bot: Bot):
    """
    Отправляет напоминания о преимуществах PRO пользователям,
    у которых подписка ещё не активирована.
    """
    try:
        async with AsyncSessionLocal() as db:
            # Универсальный запрос, совместимый с любой версией таблицы users
            stmt = text("""
                SELECT telegram_id 
                FROM users 
                WHERE (is_pro = false OR is_pro IS NULL)
                  AND telegram_id IS NOT NULL
                LIMIT 50;
            """)
            users = (await db.execute(stmt)).mappings().all()

            for u in users:
                msg = (
                    "⭐ <b>Экономьте до 3000 ₽ в месяц с BinzoLife PRO!</b>\n\n"
                    "Вам доступны эксклюзивные возможности:\n"
                    "🔹 <b>Пятничный радар</b> — подборка выгодных цен перед выходными\n"
                    "🔹 <b>Экстренный поиск</b> — поиск ближайшей проверенной АЗС в 1 клик\n"
                    "🔹 <b>Мониторинг наличия</b> и очередей на колонках в реальном времени\n\n"
                    "🚀 <i>Оформите подписку в меню /profile и заправляйтесь с максимальной выгодой!</i>"
                )
                try:
                    await bot.send_message(
                        chat_id=u["telegram_id"],
                        text=msg,
                        parse_mode="HTML"
                    )
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"[ProNotifications] Предупреждение в send_pro_onboarding_reminders: {e}")


# =====================================================================
# 2. ФОНОВЫЙ ВОРКЕР ДЛЯ MAIN.PY
# =====================================================================

async def pro_reminder_worker(bot: Bot):
    """
    Фоновый воркер напоминаний о PRO (проверка раз в 24 часа).
    """
    logger.info("[ProNotifications] Воркер напоминаний PRO успешно запущен.")
    await asyncio.sleep(90)
    while True:
        try:
            await send_pro_onboarding_reminders(bot)
        except asyncio.CancelledError:
            logger.info("[ProNotifications] Воркер напоминаний PRO остановлен.")
            break
        except Exception as e:
            logger.error(f"[ProNotifications] Необработанная ошибка воркера: {e}")

        await asyncio.sleep(86400)


# Алиасы для полной совместимости
pro_notifications_worker = pro_reminder_worker
run_pro_reminders = pro_reminder_worker
