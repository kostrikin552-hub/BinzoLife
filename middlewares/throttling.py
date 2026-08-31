import time
from typing import Dict, Any, Awaitable, Callable
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery
from cachetools import TTLCache

class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit: float = 1.5, max_users: int = 10000, ttl: int = 300):
        """
        :param limit: минимальный интервал между запросами в секундах.
        :param max_users: максимальное количество пользователей в кеше.
        :param ttl: время жизни записи в кеше (сек).
        """
        self.limit = limit
        # Используем кеш с ограничением размера и временем жизни
        self.user_last_request = TTLCache(maxsize=max_users, ttl=ttl)

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        user_id = event.from_user.id
        current_time = time.time()
        last_time = self.user_last_request.get(user_id)
        if last_time and (current_time - last_time) < self.limit:
            # Слишком частый запрос – уведомляем пользователя
            if isinstance(event, CallbackQuery):
                await event.answer("⏳ Не так часто! Подождите немного.", show_alert=False)
            elif isinstance(event, Message):
                await event.answer("⏳ Пожалуйста, не спешите. Подождите 1.5 секунды.")
            return  # Прерываем обработку
        self.user_last_request[user_id] = current_time
        return await handler(event, data)
