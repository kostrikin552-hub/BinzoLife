# utils/task_locks.py
import time
from typing import Dict

class ExpiringTaskLock:
    """Замок фоновых задач с автоматическим сбросом по таймауту (защита от дедлоков)."""

    def __init__(self):
        self._locks: Dict[str, float] = {}

    def acquire(self, task_name: str, timeout_seconds: int = 600) -> bool:
        now = time.time()
        if task_name in self._locks:
            if now < self._locks[task_name]:
                return False
        self._locks[task_name] = now + timeout_seconds
        return True

    def release(self, task_name: str) -> None:
        self._locks.pop(task_name, None)

task_locker = ExpiringTaskLock()
