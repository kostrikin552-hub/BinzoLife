from aiogram import Router, types, F
from keyboards.reply import main_menu_keyboard

router = Router()

@router.message(F.text == "◀️ Назад")
async def back_to_menu(message: types.Message):
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())

@router.message(F.text == "ℹ️ Помощь")
async def help_cmd(message: types.Message):
    await message.answer(
        "📖 Как пользоваться <b>BinzoLife</b>:\n"
        "1. Нажмите «⛽ Найти заправку» и выберите топливо.\n"
        "2. Убедитесь, что в профиле выбран ваш город (👤 Профиль → Изменить город).\n"
        "3. Получите рейтинг АЗС с ценами, наличием и расстоянием.\n"
        "4. Подтверждайте наличие на АЗС, чтобы помогать другим.\n"
        "5. Настраивайте уведомления о ценах (доступно в PRO).\n\n"
        "Все данные имеют временную метку — мы честно показываем свежесть информации.",
        reply_markup=main_menu_keyboard()
    )
