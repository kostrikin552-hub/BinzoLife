from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# ---------- Клавиатура для выбора города ----------
def city_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📍 Определить по IP", callback_data="city_by_ip")],
        [InlineKeyboardButton(text="✏️ Ввести вручную", callback_data="city_manual")],
        [InlineKeyboardButton(text="🚀 Сразу искать", callback_data="search_now")]
    ])

# ---------- Клавиатура для приветствия (первое сообщение) ----------
def welcome_keyboard() -> InlineKeyboardMarkup:
    return city_choice_keyboard()  # та же самая, для удобства

# ---------- Клавиатура для повторного запуска (Reply) ----------
def welcome_back_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="⛽ Найти заправку")],
        [KeyboardButton(text="🚨 Бензин заканчивается!")],
        [KeyboardButton(text="👤 Профиль")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# ---------- Основное меню (полное) ----------
def main_menu_keyboard() -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="⛽ Найти заправку")],
        [KeyboardButton(text="🚨 Бензин заканчивается!")],
        [KeyboardButton(text="🔔 Мои уведомления"), KeyboardButton(text="💎 PRO")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⭐ Оставить отзыв")],
        [KeyboardButton(text="ℹ️ Помощь")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

# ---------- Клавиатура для выбора сортировки ----------
def sort_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 По рейтингу", callback_data="sort_rating")],
        [InlineKeyboardButton(text="💰 По минимальной цене", callback_data="sort_price")],
        [InlineKeyboardButton(text="📍 По близости", callback_data="sort_distance")]
    ])

# ---------- Клавиатура для карточки АЗС ----------
def station_action_keyboard(station_id: int, price: float, availability_status, lat: float, lon: float, city_id: int = None, is_pro: bool = False):
    yandex_url = f"https://yandex.ru/maps/?pt={lon},{lat}&z=15"
    buttons = [
        [InlineKeyboardButton(text="🗺 Маршрут", url=yandex_url)],
        [
            InlineKeyboardButton(text="📋 Показать ещё 2 варианта", callback_data=f"more_{station_id}"),
            InlineKeyboardButton(text="✏️ Сообщить цену", callback_data=f"report_price_{station_id}")
        ]
    ]
    if is_pro:
        buttons.append([
            InlineKeyboardButton(text="📊 График цен", callback_data=f"graph_{station_id}"),
            InlineKeyboardButton(text="🟢 Увед. о появлении", callback_data=f"alert_avail_{station_id}")
        ])
        buttons.append([
            InlineKeyboardButton(text="📉 Следить за ценой", callback_data=f"follow_{station_id}")
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text="🔒 PRO-функции", callback_data="show_pro")
        ])
    if city_id:
        map_url = f"https://yandex.ru/maps/?mode=search&text=АЗС&ll={lon},{lat}&z=13"
        buttons.append([InlineKeyboardButton(text="🗺 Показать все АЗС на карте", url=map_url)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ---------- Остальные клавиатуры ----------
def notification_action_keyboard(notif_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отписаться", callback_data=f"unsub_{notif_id}")]
    ])

def pro_purchase_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 99 ₽", callback_data="buy_pro")]
    ])

def fuel_choice_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⛽ АИ-95")]],
        resize_keyboard=True
    )
