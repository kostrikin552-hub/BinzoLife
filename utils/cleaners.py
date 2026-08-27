import re

def normalize_name(name: str) -> str:
    """Очищает название АЗС от цен, дат и лишней информации"""
    if not name:
        return ""
    name = re.sub(r'\b(?:Аи|АИ)-9[258]\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bДТ\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bАи-100\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bПремиум\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\d{4}-\d{2}-\d{2}', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def clean_address(addr: str, max_length: int = 255) -> str:
    """
    Очищает адрес от HTML-тегов, цен, дат и лишней информации.
    Возвращает пустую строку, если после очистки адрес пуст или содержит только мусор.
    """
    if not addr:
        return ""

    # 1. Удаляем HTML-теги
    addr = re.sub(r'<[^>]+>', '', addr)

    # 2. Удаляем все блоки с ценами (в любом формате)
    # Сначала удаляем полные блоки с "руб." или "₽" и скобками
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*руб\.?\s*\([^)]*\)\s*', '', addr, flags=re.I)
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*руб\.?\s*', '', addr, flags=re.I)
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*₽\s*', '', addr, flags=re.I)
    # Удаляем отдельно стоящие числа с рублями
    addr = re.sub(r'\b[\d.]+\s*руб\.?', '', addr, flags=re.I)
    addr = re.sub(r'\b[\d.]+\s*₽', '', addr, flags=re.I)

    # 3. Удаляем оставшиеся "Аи-", "АИ-" и т.п., даже если после них нет цены
    # Это ловит случаи типа "Аи-Аи-" или "Аи-"
    addr = re.sub(r'\bАи-[А-Яа-я0-9]*\s*', '', addr, flags=re.I)
    addr = re.sub(r'\bАИ-[А-Яа-я0-9]*\s*', '', addr, flags=re.I)
    addr = re.sub(r'\bДТ\s*', '', addr, flags=re.I)
    addr = re.sub(r'\bПремиум\s*', '', addr, flags=re.I)

    # 4. Удаляем даты
    addr = re.sub(r'\d{4}-\d{2}-\d{2}', '', addr)

    # 5. Убираем лишние пробелы и разделители
    addr = re.sub(r'\s+', ' ', addr).strip()
    addr = re.sub(r'^[·,\s]+', '', addr)
    addr = re.sub(r'[·,\s]+$', '', addr)

    # Если осталась строка, начинающаяся с "·" или пустая – адреса нет
    if not addr or addr.startswith('·'):
        return ""

    if len(addr) > max_length:
        addr = addr[:max_length]
    return addr

def get_brand_from_name(name: str) -> str:
    """Извлекает бренд из названия"""
    name_lower = name.lower()
    brands = ['Лукойл', 'Газпромнефть', 'КрасноярскНП', 'Кит', 'ОПТИ', 'Роснефть', 'ТНК', 'Shell', 'BP', 'Tatneft', 'СКОН', 'Varta']
    for brand in brands:
        if brand.lower() in name_lower:
            return brand
    return None

def is_valid_price(price: float) -> bool:
    """Проверяет, что цена находится в разумном диапазоне (30–200 руб.)"""
    return 30.0 <= price <= 200.0
