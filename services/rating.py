from typing import Dict, Any
from datetime import datetime, timezone
from database.models import Station, FuelPrice, AvailabilityReport, AvailabilityStatus
from utils.time_utils import ensure_utc

def calculate_rating(
    station: Station,
    price_record: FuelPrice,
    availability_record: AvailabilityReport,
    avg_price_30d: float,
    min_price_30d: float,
    max_price_30d: float
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)

    # 1. Ценовая привлекательность (60%)
    price_score = 0.0
    if avg_price_30d > 0:
        deviation = (avg_price_30d - price_record.price) / avg_price_30d
        price_score = min(deviation / 0.10, 1.0)
        if min_price_30d > 0 and max_price_30d > min_price_30d:
            position = (price_record.price - min_price_30d) / (max_price_30d - min_price_30d)
            price_score = max(price_score, 1.0 - position)
    price_score = max(0.0, min(1.0, price_score))

    # 2. Наличие и свежесть (40%)
    freshness_score = 0.0
    age = 0
    if availability_record:
        rec_time = ensure_utc(availability_record.recorded_at)
        age = (now - rec_time).total_seconds() / 3600
        freshness_score = max(0.0, 1.0 - age / 24.0)
        confidence_score = availability_record.confidence
        status_score = {
            AvailabilityStatus.GREEN: 1.0,
            AvailabilityStatus.YELLOW: 0.6,
            AvailabilityStatus.RED: 0.0,
            AvailabilityStatus.GRAY: 0.3,
        }.get(availability_record.status, 0.3)
        availability_score = (freshness_score * 0.5 + confidence_score * 0.3 + status_score * 0.2)
    else:
        availability_score = 0.2

    total = (price_score * 0.6 + availability_score * 0.4) * 100
    rating = round(min(100, total))

    # ---- Расчёт разницы в рублях ----
    diff = avg_price_30d - price_record.price if avg_price_30d > 0 else 0
    if diff > 0.5:
        price_text = f"дешевле на {diff:.2f} ₽"
    elif diff < -0.5:
        price_text = f"дороже на {abs(diff):.2f} ₽"
    else:
        price_text = "цена близка к средней"

    reasons = []
    reasons.append(price_text)

    if availability_record and availability_record.status == AvailabilityStatus.GREEN and freshness_score > 0.7:
        reasons.append(f"наличие подтверждено {round(age)} мин назад")
    elif availability_record and availability_record.status == AvailabilityStatus.GREEN:
        reasons.append("наличие подтверждено, но данные не свежие")
    elif availability_record and availability_record.status == AvailabilityStatus.GRAY:
        if age > 2:
            reasons.append(f"наличие неизвестно (данные старше {round(age)} ч)")
        else:
            reasons.append("наличие неизвестно")
    elif availability_record and availability_record.status == AvailabilityStatus.RED:
        reasons.append("наличие отсутствует (подтверждено)")

    return {
        "rating": rating,
        "price": price_record.price,
        "price_time": price_record.recorded_at,
        "availability": availability_record.status if availability_record else AvailabilityStatus.GRAY,
        "availability_time": availability_record.recorded_at if availability_record else None,
        "explanation": "; ".join(reasons),
        "price_diff": diff,  # для использования в карточке
        "avg_price": avg_price_30d,
    }
