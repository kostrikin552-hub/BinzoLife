from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

def main_menu_keyboard():
    buttons = [
        [KeyboardButton(text="⛽ Найти заправку")],
        [KeyboardButton(text="🔔 Мои уведомления"), KeyboardButton(text="💎 PRO")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⭐ Оставить отзыв")],
        [KeyboardButton(text="ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def fuel_choice_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⛽ АИ-95")]],
        resize_keyboard=True
    )
