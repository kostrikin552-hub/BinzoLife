from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def station_action_keyboard(station_id: int, price: float, availability_status: str, lat: float, lon: float):
    """Клавиатура для результатов поиска АЗС – только кнопка маршрута."""
    yandex_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=15"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚗 Маршрут", url=yandex_url)]
        # Кнопки "Следить за ценой" и "Подробнее" удалены
    ])

# Остальные клавиатуры (для уведомлений и PRO) остаются без изменений
def notification_action_keyboard(notif_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отписаться", callback_data=f"unsub_{notif_id}")]
    ])

def pro_purchase_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 99 ₽", callback_data="buy_pro")]
    ])
