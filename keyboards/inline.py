# keyboards/inline.py — ПОЛНАЯ ОБНОВЛЁННАЯ ВЕРСИЯ (компактная сетка 1-2-1-2-2-1)
from typing import Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


# ====================== ВЫБОР ГОРОДА ======================
def city_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Ввести название города", callback_data="input_city_name")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
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
    for city in cities:
        row.append(InlineKeyboardButton(text=city, callback_data=f"city_select_{city}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    if with_back:
        buttons.append([InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_to_profile")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_cities_keyboard(cities: list, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(cities) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    batch = cities[start:start + per_page]
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


# ====================== ВЫБОР ТОПЛИВА ======================
def get_fuel_selection_keyboard(selected_fuel: str = "АИ-95") -> InlineKeyboardMarkup:
    fuels = ["АИ-92", "АИ-95", "АИ-98", "АИ-100", "ДТ"]
    buttons = []
    row = []
    for f in fuels:
        label = f"✅ {f}" if f == selected_fuel else f
        row.append(InlineKeyboardButton(text=label, callback_data=f"fuel_{f}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ====================== КАРТОЧКА АЗС (компактная сетка 1-2-1-2-2-1) ======================
def station_action_keyboard(
    station_id: int,
    price: float,
    availability,
    lat: float,
    lon: float,
    city_id: int = None,
    is_pro: bool = False,
    index: int = 0,
    total: int = 1,
    fuel_type: str = "АИ-95",
    partner_url: Optional[str] = None
) -> InlineKeyboardMarkup:
    """
    Компактная клавиатура карточки АЗС.
    Схема: 1-2-1-2-2-2-1 (для PRO), 1-2-1-2-2-1 (для non-PRO).
    Все callback_data сохранены — обратная совместимость с handlers/find.py.
    """
    buttons = []

    # --- Ряд 1: Главная CTA (1 кнопка) ---
    nav_url = f"https://yandex.ru/maps/?rtext=~{lat},{lon}&rtt=auto"
    buttons.append([InlineKeyboardButton(text="🚗 Поехать (Яндекс.Навигатор)", url=nav_url)])

    # --- Ряд 2: Картографические сервисы (2 кнопки) ---
    map_row = [
        InlineKeyboardButton(text="🗺 2ГИС", url=f"https://2gis.ru/geo/{lon},{lat}")
    ]
    if city_id:
        map_url = f"https://yandex.ru/maps/?mode=search&text=АЗС&ll={lon},{lat}&z=13"
        map_row.append(InlineKeyboardButton(text="📍 Все АЗС на карте", url=map_url))
    buttons.append(map_row)

    # --- Ряд 3: Партнёрская скидка (1 кнопка, если есть) ---
    if partner_url:
        buttons.append([InlineKeyboardButton(text="🎁 Скидка 20% на мойку", url=partner_url)])

    # --- Ряд 4: Выдача и аналитика (2 кнопки) ---
    info_row = [
        InlineKeyboardButton(text="📋 Показать ещё 2", callback_data=f"more_{station_id}")
    ]
    if is_pro:
        info_row.append(InlineKeyboardButton(
            text="📊 График цен",
            callback_data=f"graph_{station_id}_{fuel_type}"
        ))
    else:
        info_row.append(InlineKeyboardButton(
            text="🔒 PRO-функции",
            callback_data="show_pro"
        ))
    buttons.append(info_row)

    # --- Ряд 5 (PRO): Подписки (2 кнопки) ---
    if is_pro:
        buttons.append([
            InlineKeyboardButton(
                text=f"📉 Следить ({fuel_type})",
                callback_data=f"follow_{station_id}_{fuel_type}"
            ),
            InlineKeyboardButton(
                text=f"🔔 Увед. ({fuel_type})",
                callback_data=f"alert_avail_{station_id}_{fuel_type}"
            )
        ])

    # --- Ряд 6: Сообщить цену | Поделиться (2 кнопки) ---
    buttons.append([
        InlineKeyboardButton(
            text="✏️ Сообщить цену",
            callback_data=f"report_price_{station_id}_{fuel_type}"
        ),
        InlineKeyboardButton(
            text="📤 Поделиться",
            callback_data=f"share_{station_id}"
        )
    ])

    # --- Ряд 7: Возврат в главное меню (1 кнопка) ---
    buttons.append([InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_to_menu")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ====================== КЛАВИАТУРА ПРОФИЛЯ (компактная 2-2-2-1 / 2-2-2-2) ======================
def profile_keyboard(is_pro: bool = False) -> InlineKeyboardMarkup:
    """
    Компактная клавиатура профиля.
    Все callback_data сохранены — обратная совместимость с handlers/profile.py.
    Для non-PRO: 2-2-2-2 (8 кнопок)
    Для PRO:     2-2-2-1 (7 кнопок)
    """
    buttons = [
        [InlineKeyboardButton(text="🏙 Изменить город", callback_data="change_city"),
         InlineKeyboardButton(text="⛽ Изменить топливо", callback_data="change_fuel")],
        [InlineKeyboardButton(text="📊 Моя статистика", callback_data="stats"),
         InlineKeyboardButton(text="📜 История поисков", callback_data="search_history")],
        [InlineKeyboardButton(text="🔇 Настройка тишины", callback_data="silent_settings"),
         InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="referral_hub")],
    ]
    if not is_pro:
        buttons.append([
            InlineKeyboardButton(text="🔥 Оформить PRO", callback_data="buy_pro"),
            InlineKeyboardButton(text="◀️ В меню", callback_data="back_to_menu")
        ])
    else:
        buttons.append([InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# ====================== ПРОЧИЕ КЛАВИАТУРЫ (без изменений) ======================
def notification_action_keyboard(notif_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отписаться", callback_data=f"unsub_{notif_id}")]
    ])


def pro_purchase_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚡ 49 ₽ / 3 дня", callback_data="buy_tariff_pro_weekend"),
         InlineKeyboardButton(text="⚡ 29 ₽ / 24ч", callback_data="buy_tariff_pro_24h")],
        [InlineKeyboardButton(text="👑 99 ₽ / мес", callback_data="buy_tariff_pro_1m"),
         InlineKeyboardButton(text="🔥 249 ₽ / 3 мес", callback_data="buy_tariff_pro_3m")],
        [InlineKeyboardButton(text="💎 Подробнее о PRO", callback_data="buy_pro")]
    ])


def emergency_payment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 50 ₽", callback_data="pay_emergency_rub"),
         InlineKeyboardButton(text="⭐ Оплатить 50 Stars", callback_data="pay_emergency_stars")],
        [InlineKeyboardButton(text="🔥 Купить PRO", callback_data="buy_pro")]
    ])


def sort_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 По рейтингу", callback_data="sort_rating")],
        [InlineKeyboardButton(text="💰 По минимальной цене", callback_data="sort_price")]
    ])
