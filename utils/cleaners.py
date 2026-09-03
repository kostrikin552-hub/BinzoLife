import re

# ===== Нормализация брендов =====
BRAND_SYNONYMS = {
    "газпромнефть": ["гпн", "газпром нефть", "gpn", "газпромнефть"],
    "лукойл": ["лук", "lukoil", "лукойл"],
    "роснефть": ["рн", "rosneft", "роснефть"],
    "татнефть": ["tatneft", "татнефть"],
    "shell": ["шелл"],
    "bp": ["бипи"],
}

def normalize_brand(brand: str) -> str:
    if not brand:
        return ""
    brand_lower = brand.lower().strip()
    for canonical, aliases in BRAND_SYNONYMS.items():
        if brand_lower in aliases or brand_lower == canonical:
            return canonical
    return brand_lower

def normalize_name(name: str) -> str:
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
    if not addr:
        return ""
    addr = re.sub(r'<[^>]+>', '', addr)
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*руб\.?\s*\([^)]*\)\s*', '', addr, flags=re.I)
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*руб\.?\s*', '', addr, flags=re.I)
    addr = re.sub(r'[А-Яа-я0-9\s]+?\s*[:：]\s*[\d.]+\s*₽\s*', '', addr, flags=re.I)
    addr = re.sub(r'\b[\d.]+\s*руб\.?', '', addr, flags=re.I)
    addr = re.sub(r'\b[\d.]+\s*₽', '', addr, flags=re.I)
    addr = re.sub(r'\bАи-[А-Яа-я0-9]*\s*', '', addr, flags=re.I)
    addr = re.sub(r'\bАИ-[А-Яа-я0-9]*\s*', '', addr, flags=re.I)
    addr = re.sub(r'\bДТ\s*', '', addr, flags=re.I)
    addr = re.sub(r'\bПремиум\s*', '', addr, flags=re.I)
    addr = re.sub(r'\d{4}-\d{2}-\d{2}', '', addr)
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

ADDRESS_KEYWORDS = re.compile(
    r'(ул\.|улица|пр\.|проспект|пер\.|переулок|ш\.|шоссе|бульвар|наб\.|набережная|пл\.|площадь|д\.|дом|корп|строение|владение|пос\.|поселок|город|г\.|деревня|д\.|с\.|село|квартал|микрорайон|мкр\.|жилой|комплекс)',
    re.IGNORECASE
)

def is_likely_address(text: str) -> bool:
    if not text or not isinstance(text, str):
        return False
    text = text.strip()
    if len(text) < 5:
        return False
    if not re.search(r'[А-Яа-яA-Za-z]', text):
        return False
    if ADDRESS_KEYWORDS.search(text):
        return True
    if re.search(r'\d', text) and re.search(r'[,.]', text):
        return True
    if re.match(r'^\d{6}', text):
        return True
    return False

def normalize_brand_full(brand: str) -> str:
    """Возвращает каноническое название бренда."""
    if not brand:
        return ""
    return normalize_brand(brand)
