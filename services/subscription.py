from datetime import datetime
from database.session import AsyncSessionLocal
from database.crud import get_user, is_user_pro

async def check_pro(telegram_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        user = await get_user(db, telegram_id)
        if not user:
            return False
        return await is_user_pro(db, user)

def format_pro_until(pro_until: datetime) -> str:
    if not pro_until:
        return "не активна"
    return pro_until.strftime("%d.%m.%Y %H:%M")
