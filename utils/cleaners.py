# utils/cleaners.py – ПОЛНАЯ ИСПРАВЛЕННАЯ ВЕРСИЯ

import re

def normalize_name(name: str) -> str:
    """Очищает название АЗС от цен, дат и лишней информации"""
    if not name:
        return ""
    name = re.sub(r'\bАи-9[258]\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bАИ-9[258]\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bДТ\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bАи-100\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bПремиум\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\d{4}-\d{2}-\d{2}', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def clean_address(addr: str, max_length: int = 255) -> str:
    """
    Очищает адрес от HTML-тегов, цен, дат и лишней информации.
    Возвращает пустую строку, если после очистки адрес пуст.
    """
    if not addr:
        return ""
    # Удаляем HTML-теги
    addr = re.sub(r'<[^>]+>', '', addr)

    # Удаляем блоки с ценами в формате "Название: цена руб. ()" или "Название: цена ₽"
    # Поддерживаем: Аи-92, АИ-95, ДТ, Премиум 95, Премиум, и любые другие названия с пробелами и цифрами
    # Более агрессивно: удаляем всё, что содержит ":" или "：" и заканчивается на "руб." или "₽" с возможными скобками
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*руб\.?\s*\([^)]*\)', '', addr, flags=re.I)
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*руб\.?', '', addr, flags=re.I)
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*₽', '', addr, flags=re.I)
    # Также удаляем просто "руб." с числом без названия топлива (например, "65.61 руб.")
    addr = re.sub(r'\b[\d.]+\s*руб\.?', '', addr, flags=re.I)
    addr = re.sub(r'\b[\d.]+\s*₽', '', addr, flags=re.I)

    # Удаляем даты
    addr = re.sub(r'\d{4}-\d{2}-\d{2}', '', addr)

    # Убираем лишние пробелы
    addr = re.sub(r'\s+', ' ', addr).strip()

    # Убираем разделители в начале и конце (точки, запятые, тире, "·")
    addr = re.sub(r'^[·,\s]+', '', addr)
    addr = re.sub(r'[·,\s]+$', '', addr)

    # Если осталась строка, начинающаяся с "·" или пустая – адреса нет
    if not addr or addr.startswith('·'):
        return ""

    # Обрезаем до max_length
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
