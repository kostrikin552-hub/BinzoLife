from typing import Dict, Any, Awaitable, Callable
from aiogram import BaseMiddleware
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from keyboards.reply import main_menu_keyboard

class ClearStateOnMenuMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        # Получаем все тексты кнопок из главного меню
        menu_buttons = []
        for row in main_menu_keyboard().keyboard:
            for btn in row:
                menu_buttons.append(btn.text)

        if event.text and event.text in menu_buttons:
            state: FSMContext = data.get("state")
            if state:
                await state.clear()
        return await handler(event, data)
