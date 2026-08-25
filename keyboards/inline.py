from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

def city_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Выбрать город из списка", callback_data="city_list")]
    ])

def popular_cities_keyboard(with_back: bool = False) -> InlineKeyboardMarkup:
    cities = [
        "Москва", "Санкт-Петербург", "Новосибирск", "Екатеринбург",
        "Казань", "Нижний Новгород", "Челябинск", "Омск",
        "Самара", "Ростов-на-Дону", "Уфа", "Красноярск",
        "Пермь", "Воронеж", "Волгоград", "Тула"
    ]
    buttons = []
    row = []
    for i, city in enumerate(cities):
        row.append(InlineKeyboardButton(text=city, callback_data=f"city_select_{city}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    if with_back:
        buttons.append([InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_to_profile")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def welcome_back_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="⛽ Найти заправку")],
        [KeyboardButton(text="🚨 Бензин заканчивается!")],
        [KeyboardButton(text="👤 Профиль")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="⛽ Найти заправку")],
        [KeyboardButton(text="🚨 Бензин заканчивается!")],
        [KeyboardButton(text="🔔 Мои уведомления"), KeyboardButton(text="💎 PRO")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⭐ Оставить отзыв")],
        [KeyboardButton(text="ℹ️ Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def sort_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 По рейтингу", callback_data="sort_rating")],
        [InlineKeyboardButton(text="💰 По минимальной цене", callback_data="sort_price")]
    ])

def station_action_keyboard(station_id: int, price: float, availability, lat: float, lon: float, city_id: int = None, is_pro: bool = False, index: int = 0, total: int = 1):
    yandex_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=15"
    buttons = [
        [InlineKeyboardButton(text="🗺 Маршрут", url=yandex_url)]
    ]
    buttons.append([InlineKeyboardButton(text="📋 Показать ещё 2 варианта", callback_data=f"more_{station_id}")])
    buttons.append([InlineKeyboardButton(text="✏️ Сообщить цену", callback_data=f"report_price_{station_id}")])

    # НОВАЯ КНОПКА ПОДЕЛИТЬСЯ
    buttons.append([InlineKeyboardButton(text="📤 Поделиться с друзьями", callback_data=f"share_{station_id}")])

    if is_pro:
        buttons.append([InlineKeyboardButton(text="📊 График цен", callback_data=f"graph_{station_id}")])
        buttons.append([InlineKeyboardButton(text="🟢 Увед. о появлении", callback_data=f"alert_avail_{station_id}")])
        buttons.append([InlineKeyboardButton(text="📉 Следить за ценой", callback_data=f"follow_{station_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="🔒 PRO-функции", callback_data="show_pro")])

    if city_id:
        map_url = f"https://yandex.ru/maps/?mode=search&text=АЗС&ll={lon},{lat}&z=13"
        buttons.append([InlineKeyboardButton(text="🗺 Показать все АЗС на карте", url=map_url)])

    return InlineKeyboardMarkup(inline_keyboard=buttons)

def notification_action_keyboard(notif_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отписаться", callback_data=f"unsub_{notif_id}")]
    ])

def pro_purchase_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 99 ₽", callback_data="buy_pro")],
        [InlineKeyboardButton(text="⭐ Оплатить 150 Stars", callback_data="buy_pro_stars")]
    ])

def fuel_choice_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⛽ АИ-95")]],
        resize_keyboard=True
    )

def emergency_payment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 50 ₽", callback_data="pay_emergency_rub")],
        [InlineKeyboardButton(text="⭐ Оплатить 50 Stars", callback_data="pay_emergency_stars")],
        [InlineKeyboardButton(text="🔥 Купить PRO", callback_data="buy_pro")]
    ])
