# handlers/common.py — с полной трассировкой ошибок
import logging
import traceback
from aiogram import Router, types, F
from keyboards.reply import main_menu_keyboard

router = Router()

@router.message(F.text == "◀️ Назад")
async def back_to_menu(message: types.Message):
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())

@router.error()
async def error_handler(event: types.ErrorEvent):
    logging.error(
        f"❌ Ошибка в обработчике:\n"
        f"Тип: {type(event.exception).__name__}\n"
        f"Сообщение: {event.exception}\n"
        f"Трассировка:\n{traceback.format_exc()}"
    )
