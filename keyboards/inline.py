# keyboards/inline.py — ПОЛНАЯ ВЕРСИЯ (с пагинацией городов)
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


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


def get_cities_keyboard(cities: list, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    """Инлайн-клавиатура выбора города с постраничной навигацией."""
    total_pages = max(1, (len(cities) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = start + per_page
    batch = cities[start:end]
    keyboard = []
    row = []
    for c in batch:
        row.append(InlineKeyboardButton(text=c.name, callback_data=f"select_city_{c.id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"cities_page_{page - 1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"Стр. {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"cities_page_{page + 1}"))
    keyboard.append(nav_buttons)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_fuel_selection_keyboard(selected_fuel: str = "АИ-95") -> InlineKeyboardMarkup:
    fuels = ["АИ-92", "АИ-95", "АИ-98", "АИ-100", "ДТ"]
    buttons = []
    for f in fuels:
        label = f"✅ {f}" if f == selected_fuel else f
        buttons.append(InlineKeyboardButton(text=label, callback_data=f"fuel_{f}"))
    return InlineKeyboardMarkup(inline_keyboard=[buttons[:3], buttons[3:]])


def station_action_keyboard(station_id: int, price: float, availability, lat: float, lon: float,
                            city_id: int = None, is_pro: bool = False, index: int = 0,
                            total: int = 1, fuel_type: str = "АИ-95"):
    nav_url = f"https://yandex.ru/maps/?rtext=~{lat},{lon}&rtt=auto"
    buttons = [
        [InlineKeyboardButton(text="🚗 Поехать (Яндекс.Навигатор)", url=nav_url)],
        [InlineKeyboardButton(text="🗺 2ГИС", url=f"https://2gis.ru/geo/{lon},{lat}")]
    ]
    buttons.append([InlineKeyboardButton(text="📋 Показать ещё 2 варианта", callback_data=f"more_{station_id}")])
    buttons.append([InlineKeyboardButton(text="✏️ Сообщить цену", callback_data=f"report_price_{station_id}")])
    buttons.append([InlineKeyboardButton(text="📤 Поделиться с друзьями", callback_data=f"share_{station_id}")])

    if is_pro:
        buttons.append([InlineKeyboardButton(text="📊 График цен", callback_data=f"graph_{station_id}")])
        buttons.append([InlineKeyboardButton(text=f"🟢 Увед. о появлении ({fuel_type})", callback_data=f"alert_avail_{station_id}_{fuel_type}")])
        buttons.append([InlineKeyboardButton(text=f"📉 Следить за ценой ({fuel_type})", callback_data=f"follow_{station_id}_{fuel_type}")])
    else:
        buttons.append([InlineKeyboardButton(text="🔒 PRO-функции", callback_data="show_pro")])

    if city_id:
        map_url = f"https://yandex.ru/maps/?mode=search&text=АЗС&ll={lon},{lat}&z=13"
        buttons.append([InlineKeyboardButton(text="🗺 Показать все АЗС на карте", url=map_url)])

    buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_to_menu")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def notification_action_keyboard(notif_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отписаться", callback_data=f"unsub_{notif_id}")]
    ])


def pro_purchase_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ 49 ₽ / 3 дня (Выходные)", callback_data="buy_tariff_pro_weekend"),
         InlineKeyboardButton(text="⚡ 29 ₽ / 24ч", callback_data="buy_tariff_pro_24h")],
        [InlineKeyboardButton(text="👑 99 ₽ / мес", callback_data="buy_tariff_pro_1m"),
         InlineKeyboardButton(text="🔥 249 ₽ / 3 мес", callback_data="buy_tariff_pro_3m")],
        [InlineKeyboardButton(text="💎 Подробнее о PRO", callback_data="buy_pro")]
    ])


def emergency_payment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 50 ₽", callback_data="pay_emergency_rub")],
        [InlineKeyboardButton(text="⭐ Оплатить 50 Stars", callback_data="pay_emergency_stars")],
        [InlineKeyboardButton(text="🔥 Купить PRO", callback_data="buy_pro")]
    ])


def sort_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 По рейтингу", callback_data="sort_rating")],
        [InlineKeyboardButton(text="💰 По минимальной цене", callback_data="sort_price")]
    ])


def welcome_back_keyboard() -> InlineKeyboardMarkup:
    # Уже используется из reply.py, но оставим для совместимости
    pass
