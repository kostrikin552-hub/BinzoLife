import logging
from datetime import datetime, timezone
from sqlalchemy import text
from database.session import AsyncSessionLocal

logger = logging.getLogger(__name__)

class TASK_NAMES:
    NOTIFICATIONS = "notifications"
    PRICES = "prices"
    EXPIRE_DATA = "expire_data"
    ACHIEVEMENTS = "achievements"
    FUNNEL = "funnel"
    RESET_VIEWS = "reset_views"
    ADDRESS_UPDATER = "address_updater"
    PRO_NOTIFY = "pro_notify"

async def acquire_lock(task_name: str, lock_timeout_seconds: int = 300) -> bool:
    """
    Попытка захватить блокировку для задачи.
    Возвращает True, если блокировка успешно захвачена.
    Если блокировка существует и старше lock_timeout_seconds, она освобождается.
    """
    async with AsyncSessionLocal() as db:
        # Проверяем, есть ли блокировка
        row = await db.execute(
            text("SELECT locked_at, locked_by FROM task_locks WHERE task_name = :task_name"),
            {"task_name": task_name}
        )
        record = row.first()
        if record:
            locked_at = record[0]
            if locked_at:
                age = (datetime.now(timezone.utc) - locked_at).total_seconds()
                if age < lock_timeout_seconds:
                    return False  # блокировка ещё активна
                else:
                    # Блокировка истекла — освобождаем
                    await db.execute(
                        text("DELETE FROM task_locks WHERE task_name = :task_name"),
                        {"task_name": task_name}
                    )
                    await db.commit()
        # Захватываем блокировку
        await db.execute(
            text("""
                INSERT INTO task_locks (task_name, locked_at, locked_by)
                VALUES (:task_name, NOW(), :locked_by)
                ON CONFLICT (task_name) DO UPDATE
                SET locked_at = NOW(), locked_by = :locked_by
            """),
            {"task_name": task_name, "locked_by": "unknown"}
        )
        await db.commit()
        return True

async def release_lock(task_name: str):
    """Освобождает блокировку."""
    async with AsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM task_locks WHERE task_name = :task_name"),
            {"task_name": task_name}
        )
        await db.commit()
