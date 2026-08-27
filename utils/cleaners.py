# utils/cleaners.py – НОВЫЙ МОДУЛЬ ДЛЯ ОЧИСТКИ ДАННЫХ

import re

def normalize_name(name: str) -> str:
    """Очищает название АЗС от цен, дат и лишней информации"""
    if not name:
        return ""
    # Убираем все цены для разных видов топлива
    name = re.sub(r'\bАи-9[258]\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bАИ-9[258]\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bДТ\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bАи-100\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bПремиум\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    # Убираем даты (например, 2026-08-25)
    name = re.sub(r'\d{4}-\d{2}-\d{2}', '', name)
    # Убираем лишние пробелы
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def clean_address(addr: str, max_length: int = 255) -> str:
    """Очищает адрес от HTML-тегов, цен, дат и лишней информации"""
    if not addr:
        return ""
    # Удаляем HTML-теги (все, что между < и >)
    addr = re.sub(r'<[^>]+>', '', addr)
    # Удаляем цены для всех видов топлива
    addr = re.sub(r'Аи-[0-9]+[:：][\d.]+\s*₽', '', addr, flags=re.I)
    addr = re.sub(r'АИ-[0-9]+[:：][\d.]+\s*₽', '', addr, flags=re.I)
    addr = re.sub(r'ДТ[:：][\d.]+\s*₽', '', addr, flags=re.I)
    addr = re.sub(r'Премиум[0-9]*[:：][\d.]+\s*₽', '', addr, flags=re.I)
    # Удаляем даты
    addr = re.sub(r'\d{4}-\d{2}-\d{2}', '', addr)
    # Убираем лишние пробелы
    addr = re.sub(r'\s+', ' ', addr).strip()
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
