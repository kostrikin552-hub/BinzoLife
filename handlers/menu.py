from aiogram import Router, types, F
from keyboards.reply import main_menu_keyboard

router = Router()

@router.message(F.text == "◀️ Назад")
async def back_to_menu(message: types.Message):
    await message.answer("Главное меню:", reply_markup=main_menu_keyboard())

@router.message(F.text == "ℹ️ Помощь")
async def help_cmd(message: types.Message):
    text = (
        "📖 <b>Как BinzoLife сэкономит тебе время и деньги:</b>\n\n"
        "1️⃣ <b>Найди АЗС за 10 секунд</b>\n"
        "Нажми «⛽ Найти заправку», выбери топливо — и я покажу лучшие варианты с ценой, наличием и маршрутом.\n\n"
        "2️⃣ <b>Бензин на нуле?</b>\n"
        "Нажми «🚨 Бензин заканчивается!» — я найду ближайшую АЗС с топливом даже в час пик.\n\n"
        "3️⃣ <b>Помогай другим водителям</b>\n"
        "Сообщай актуальные цены через «✏️ Сообщить цену» — получай репутацию и бонусные дни PRO.\n\n"
        "4️⃣ <b>Не упускай выгоду</b>\n"
        "С PRO ты будешь получать уведомления о снижении цен и появлении топлива на любимых АЗС. Экономь до 500 ₽ на каждой заправке!\n\n"
        "💡 Все данные обновляются в реальном времени и имеют временную метку — я честен с тобой.\n\n"
        "🚀 <b>Готов начать экономить?</b> Просто нажми «Найти заправку»!"
    )
    await message.answer(text, reply_markup=main_menu_keyboard(), parse_mode="HTML")

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: types.CallbackQuery):
    await callback.answer()
    await callback.message.delete()
    await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard())
