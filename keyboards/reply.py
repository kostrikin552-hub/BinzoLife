# keyboards/reply.py — ПОЛНАЯ ВЕРСИЯ
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton


def main_menu_keyboard():
    buttons = [
        [KeyboardButton(text="⛽ Найти заправку")],
        [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
        [KeyboardButton(text="🚨 Бензин заканчивается!")],
        [KeyboardButton(text="🔔 Мои уведомления"), KeyboardButton(text="💎 PRO")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⭐ Оставить отзыв")],
        [KeyboardButton(text="ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def welcome_back_keyboard():
    buttons = [
        [KeyboardButton(text="⛽ Найти заправку")],
        [KeyboardButton(text="📍 Отправить геолокацию", request_location=True)],
        [KeyboardButton(text="🚨 Бензин заканчивается!")],
        [KeyboardButton(text="👤 Профиль")],
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def fuel_choice_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⛽ АИ-92"), KeyboardButton(text="⛽ АИ-95")],
            [KeyboardButton(text="⛽ АИ-98"), KeyboardButton(text="⛽ АИ-100")],
            [KeyboardButton(text="⛽ ДТ")],
            [KeyboardButton(text="◀️ Назад")],
        ],
        resize_keyboard=True
    )


def sort_choice_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔥 По рейтингу")],
            [KeyboardButton(text="💰 По минимальной цене")],
            [KeyboardButton(text="◀️ Назад")],
        ],
        resize_keyboard=True
    )
