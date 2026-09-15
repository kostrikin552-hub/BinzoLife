# keyboards/reply.py — ОБНОВЛЁННАЯ ВЕРСИЯ (без «Бензин заканчивается!» и «Отправить геолокацию» в главном меню)
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="⛽ Найти заправку")],
        [KeyboardButton(text="🔔 Мои уведомления"), KeyboardButton(text="💎 PRO")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⭐ Оставить отзыв")],
        [KeyboardButton(text="ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def welcome_back_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="⛽ Найти заправку")],
        [KeyboardButton(text="👤 Профиль")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def fuel_choice_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⛽ АИ-92"), KeyboardButton(text="⛽ АИ-95")],
            [KeyboardButton(text="⛽ АИ-98"), KeyboardButton(text="⛽ АИ-100")],
            [KeyboardButton(text="⛽ ДТ")],
            [KeyboardButton(text="◀️ Назад")],
        ],
        resize_keyboard=True
    )


def request_geo_or_city_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура выбора города: только GPS и текстовый ввод (используется в смене города)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Определить город по GPS", request_location=True)],
            [KeyboardButton(text="✏️ Написать город текстом")],
            [KeyboardButton(text="◀️ В главное меню")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
