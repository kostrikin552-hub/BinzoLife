# services/subscription.py — ПОЛНАЯ ВЕРСИЯ (исправлена ошибка text)
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Union, Dict, Any
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import text, select, and_
from database.session import AsyncSessionLocal
from database.models import User
from database.crud import commit_or_rollback, get_user_by_id, is_user_pro

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
# 2. ПРОВЕРКА СТАТУСА ПОДПИСКИ
# =====================================================================
async def is_user_pro_by_id(db, user_id: int) -> bool:
    user = await get_user_by_id(db, user_id)
    if not user:
        return False
    return await is_user_pro(db, user)

async def check_pro(telegram_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        from database.crud import get_user
        user = await get_user(db, telegram_id)
        if not user:
            return False
        return await is_user_pro(db, user)

# =====================================================================
# 3. ФОНОВЫЙ ВОРКЕР: ПРОВЕРКА ИСТЕКАЮЩИХ PRO И ТРИАЛА
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
                    msg = (
                        "⏳ <b>Ваша PRO-подписка истекает через 3 дня!</b>\n\n"
                        "Продлите подписку в меню /profile, чтобы не потерять доступ."
                    )
                    await bot.send_message(u["telegram_id"], msg, parse_mode="HTML")
                    await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    await db.execute(text("UPDATE users SET is_active = false WHERE telegram_id = :uid"), {"uid": u["telegram_id"]})
                    await commit_or_rollback(db)
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
                    msg = (
                        "⚠️ <b>Внимание: PRO-подписка заканчивается завтра!</b>\n\n"
                        "Продлите подписку в разделе /profile."
                    )
                    await bot.send_message(u["telegram_id"], msg, parse_mode="HTML")
                    await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    await db.execute(text("UPDATE users SET is_active = false WHERE telegram_id = :uid"), {"uid": u["telegram_id"]})
                    await commit_or_rollback(db)
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
            await commit_or_rollback(db)

            for u in expired:
                try:
                    msg = (
                        "❌ <b>Срок действия вашей PRO-подписки завершён.</b>\n\n"
                        "Возобновить доступ можно через команду /profile."
                    )
                    await bot.send_message(u["telegram_id"], msg, parse_mode="HTML")
                    await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    await db.execute(text("UPDATE users SET is_active = false WHERE telegram_id = :uid"), {"uid": u["telegram_id"]})
                    await commit_or_rollback(db)
                except Exception:
                    pass

            # 4. Уведомление об окончании триала (за 24 часа)
            now = datetime.now(timezone.utc)
            warning_start = now + timedelta(hours=23)
            warning_end = now + timedelta(hours=25)
            stmt_trial = text("""
                SELECT id, telegram_id, first_name, total_saved
                FROM users 
                WHERE trial_used = true 
                  AND is_pro = true 
                  AND pro_until BETWEEN :start AND :end
                  AND trial_alert_sent = false
            """)
            expiring_trials = (await db.execute(stmt_trial, {"start": warning_start, "end": warning_end})).mappings().all()

            for u in expiring_trials:
                saved_rub = u.get("total_saved") or 350.0
                first_name = u.get("first_name") or "Водитель"
                try:
                    msg = (
                        f"⏳ <b>{first_name}, ваш 3-дневный тест PRO заканчивается завтра!</b>\n\n"
                        f"За время поездок с BinzoLife вы сохранили в кошельке: <b>~{saved_rub:.0f} ₽</b>.\n\n"
                        f"Чтобы не терять утренний радар цен и SOS-помощь, активируйте постоянный PRO "
                        f"по специальной цене для ранних водителей:\n"
                        f"⭐️ <b>149 ₽/мес</b> вместо <s>199 ₽</s> (всего 5 ₽ в день)!\n\n"
                        f"<i>Спеццена сгорит вместе с окончанием триала ровно через 24 часа.</i>"
                    )
                    await bot.send_message(u["telegram_id"], msg, parse_mode="HTML")
                    await db.execute(
                        text("UPDATE users SET trial_alert_sent = true WHERE id = :uid"),
                        {"uid": u["id"]}
                    )
                    await commit_or_rollback(db)
                    await asyncio.sleep(0.05)
                except TelegramForbiddenError:
                    await db.execute(text("UPDATE users SET is_active = false WHERE telegram_id = :uid"), {"uid": u["telegram_id"]})
                    await commit_or_rollback(db)
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления о триале {u['telegram_id']}: {e}")

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
        await asyncio.sleep(43200)  # 12 часов

subscription_worker = subscription_expiration_worker
run_subscription_worker = subscription_expiration_worker
