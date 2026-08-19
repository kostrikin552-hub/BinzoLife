import math
from typing import Dict, Any
from datetime import datetime, timezone
from database.models import Station, FuelPrice, AvailabilityReport, AvailabilityStatus
from utils.helpers import haversine_distance

def calculate_rating(
    station: Station,
    user_lat: float,
    user_lon: float,
    price_record: FuelPrice,
    availability_record: AvailabilityReport,
    avg_price_30d: float,
    min_price_30d: float,
    max_price_30d: float
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)

    # 1. Ценовая привлекательность (50%)
    price_score = 0.0
    if avg_price_30d > 0:
        deviation = (avg_price_30d - price_record.price) / avg_price_30d
        price_score = min(deviation / 0.10, 1.0)
        if min_price_30d > 0 and max_price_30d > min_price_30d:
            position = (price_record.price - min_price_30d) / (max_price_30d - min_price_30d)
            price_score = max(price_score, 1.0 - position)
    price_score = max(0.0, min(1.0, price_score))

    # 2. Наличие и свежесть (20%)
    freshness_score = 0.0
    age = 0
    if availability_record:
        rec_time = availability_record.recorded_at
        if rec_time.tzinfo is None:
            rec_time = rec_time.replace(tzinfo=timezone.utc)
        age = (now - rec_time).total_seconds() / 3600
        freshness_score = max(0.0, 1.0 - age / 24.0)
        confidence_score = availability_record.confidence
        status_score = {
            AvailabilityStatus.GREEN: 1.0,
            AvailabilityStatus.YELLOW: 0.6,
            AvailabilityStatus.RED: 0.0,
            AvailabilityStatus.GRAY: 0.3,
        }.get(availability_record.status, 0.3)
        availability_score = (freshness_score * 0.4 + confidence_score * 0.4 + status_score * 0.2)
    else:
        availability_score = 0.2

    # 3. Расстояние (20%)
    dist = haversine_distance(user_lat, user_lon, station.latitude, station.longitude)
    distance_score = max(0.0, 1.0 - dist / 5.0)

    # 4. Качество данных (10%)
    price_time = price_record.recorded_at
    if price_time.tzinfo is None:
        price_time = price_time.replace(tzinfo=timezone.utc)
    price_age = (now - price_time).total_seconds() / 3600
    data_quality = max(0.0, 1.0 - price_age / 48.0)

    total = (price_score * 0.5 + availability_score * 0.2 + distance_score * 0.2 + data_quality * 0.1) * 100
    rating = round(min(100, total))

    reasons = []
    if price_score > 0.7:
        reasons.append(f"цена ниже средней на {round((avg_price_30d - price_record.price) / avg_price_30d * 100, 1)}%")
    elif price_score > 0.4:
        reasons.append("цена близка к средней")
    else:
        reasons.append("цена выше средней")

    if availability_record and availability_record.status == AvailabilityStatus.GREEN and freshness_score > 0.7:
        reasons.append(f"наличие подтверждено {round(age)} мин назад")
    elif availability_record and availability_record.status == AvailabilityStatus.GREEN:
        reasons.append("наличие подтверждено, но данные не свежие")
    elif availability_record and availability_record.status == AvailabilityStatus.GRAY:
        reasons.append("наличие неизвестно")
    elif availability_record and availability_record.status == AvailabilityStatus.RED:
        reasons.append("наличие отсутствует (подтверждено)")

    if dist < 2:
        reasons.append(f"{round(dist,1)} км от вас (очень близко)")
    elif dist < 5:
        reasons.append(f"{round(dist,1)} км от вас")
    else:
        reasons.append(f"{round(dist,1)} км от вас (далековато)")

    return {
        "rating": rating,
        "price": price_record.price,
        "price_time": price_record.recorded_at,
        "availability": availability_record.status if availability_record else AvailabilityStatus.GRAY,
        "availability_time": availability_record.recorded_at if availability_record else None,
        "distance_km": round(dist, 1),
        "drive_time_min": round(dist / 40 * 60),
        "explanation": "; ".join(reasons[:3]),
    }
