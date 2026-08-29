import logging
from datetime import datetime
from database.session import AsyncSessionLocal
from database.crud import get_user, is_user_pro

logger = logging.getLogger(__name__)

async def check_pro(telegram_id: int) -> bool:
    """
    Проверяет, активен ли PRO у пользователя.
    Логирует результат для отладки.
    """
    async with AsyncSessionLocal() as db:
        user = await get_user(db, telegram_id)
        if not user:
            logger.warning(f"check_pro: пользователь {telegram_id} не найден")
            return False
        result = await is_user_pro(db, user)
        logger.info(f"check_pro для {telegram_id}: is_pro={user.is_pro}, pro_until={user.pro_until}, результат={result}")
        return result

def format_pro_until(pro_until: datetime) -> str:
    if not pro_until:
        return "не активна"
    return pro_until.strftime("%d.%m.%Y %H:%M")
