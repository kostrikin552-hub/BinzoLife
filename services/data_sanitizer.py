# services/data_sanitizer.py
import re
from typing import Optional

KNOWN_BRANDS = [
    ("Лукойл", ["лукойл", "lukoil"]),
    ("Газпромнефть", ["газпромнефть", "газпром нефть", "g-drive", "опти"]),
    ("Татнефть", ["татнефть", "tatneft"]),
    ("Роснефть", ["роснефть", "rosneft", "тнк", "башнефть"]),
    ("Teboil", ["teboil", "тебойл", "shell", "шелл"]),
    ("Ирбис", ["irbis", "ирбис"]),
    ("Нефтьмагистраль", ["нефтьмагистраль"]),
    ("Транснефть", ["транснефть"]),
    ("Сургутнефтегаз", ["сургутнефтегаз"]),
]


def clean_brand_name(raw_name: str) -> str:
    """
    Очищает юридический мусор и оставляет чистый бренд АЗС.
    Если бренд не распознан, возвращает "АЗС".
    """
    if not raw_name:
        return "АЗС"

    text = raw_name.strip()
    low = text.lower()

    # Поиск по известным федеральным и региональным сетям
    for clean_brand, patterns in KNOWN_BRANDS:
        for p in patterns:
            if p in low:
                return clean_brand

    # Убираем ООО, ПАО, ЗАО, кавычки и номера станций
    cleaned = re.sub(r'(?i)(ооо|зао|пао|азс|№|\bазгс\b|сеть|станция)\s*', '', text)
    cleaned = cleaned.replace('"', '').replace("'", "").strip()

    return cleaned.capitalize() if len(cleaned) >= 3 else "АЗС"


def validate_fuel_price(fuel_type: str, price: float) -> Optional[float]:
    """
    Проверяет цену на адекватность реалиям рынка РФ.
    Возвращает округлённую цену или None, если цена невалидна.
    """
    if not price or not isinstance(price, (int, float)):
        return None

    price = round(float(price), 2)

    # Газ (пропан/метан)
    if "ГАЗ" in fuel_type.upper() or "LPG" in fuel_type.upper():
        if 20.0 <= price <= 45.0:
            return price
        return None

    # Бензин и Дизель
    if 35.0 <= price <= 130.0:
        return price
    return None


def normalize_address(address: str) -> str:
    """
    Приводит адрес к читаемому виду (убирает лишние пробелы, кавычки и т.п.).
    """
    if not address:
        return ""
    cleaned = re.sub(r'\s+', ' ', address).strip()
    cleaned = cleaned.replace('"', '').replace("'", "").strip()
    return cleaned
