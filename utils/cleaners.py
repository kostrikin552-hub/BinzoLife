import re

def normalize_name(name: str) -> str:
    if not name:
        return ""
    # Объединяем все варианты цен в один проход
    name = re.sub(r'\b(?:Аи|АИ)-9[258]\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bДТ\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bАи-100\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\bПремиум\s*[:：]\s*[\d.]+\s*₽', '', name, flags=re.I)
    name = re.sub(r'\d{4}-\d{2}-\d{2}', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def clean_address(addr: str, max_length: int = 255) -> str:
    if not addr:
        return ""
    # Удаляем HTML-теги
    addr = re.sub(r'<[^>]+>', '', addr)

    # Удаляем цены с указанием топлива, но оставляем скобки, если они часть адреса
    # Удаляем только те скобки, которые идут сразу после цены с "руб." или "₽"
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*руб\.?\s*\([^)]*\)\s*', '', addr, flags=re.I)
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*руб\.?\s*', '', addr, flags=re.I)
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*₽\s*', '', addr, flags=re.I)
    # Удаляем отдельно стоящие числа с рублями
    addr = re.sub(r'\b[\d.]+\s*руб\.?', '', addr, flags=re.I)
    addr = re.sub(r'\b[\d.]+\s*₽', '', addr, flags=re.I)

    # Удаляем даты
    addr = re.sub(r'\d{4}-\d{2}-\d{2}', '', addr)

    # Убираем лишние пробелы и разделители
    addr = re.sub(r'\s+', ' ', addr).strip()
    addr = re.sub(r'^[·,\s]+', '', addr)
    addr = re.sub(r'[·,\s]+$', '', addr)

    if not addr or addr.startswith('·'):
        return ""

    if len(addr) > max_length:
        addr = addr[:max_length]
    return addr

def get_brand_from_name(name: str) -> str:
    name_lower = name.lower()
    brands = ['Лукойл', 'Газпромнефть', 'КрасноярскНП', 'Кит', 'ОПТИ', 'Роснефть', 'ТНК', 'Shell', 'BP', 'Tatneft', 'СКОН', 'Varta']
    for brand in brands:
        if brand.lower() in name_lower:
            return brand
    return None

def is_valid_price(price: float) -> bool:
    return 30.0 <= price <= 200.0
