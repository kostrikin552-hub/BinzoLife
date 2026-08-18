from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

def main_menu_keyboard():
    buttons = [
        [KeyboardButton(text="⛽ Найти заправку")],
        [KeyboardButton(text="🔔 Мои уведомления"), KeyboardButton(text="📊 Цены")],
        [KeyboardButton(text="📈 Динамика"), KeyboardButton(text="💰 Моя экономия")],
        [KeyboardButton(text="💎 PRO"), KeyboardButton(text="👤 Профиль")],
        [KeyboardButton(text="ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def fuel_choice_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⛽ АИ-95")]],
        resize_keyboard=True
    )

def location_request_keyboard():
    button = KeyboardButton(text="📍 Отправить геолокацию", request_location=True)
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True)

def confirm_availability_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🟢 Есть"), KeyboardButton(text="🔴 Нет")],
            [KeyboardButton(text="⚠️ Ограничение")]
        ],
        resize_keyboard=True
    )

def back_to_menu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="◀️ Назад")]],
        resize_keyboard=True
    )
