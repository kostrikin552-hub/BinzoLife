import math
from datetime import datetime, timezone
from utils.time_utils import ensure_utc

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def format_time_ago(dt: datetime) -> str:
    if not dt:
        return "неизвестно"
    dt = ensure_utc(dt)
    now = datetime.now(timezone.utc)
    diff = now - dt
    seconds = diff.total_seconds()
    if seconds < 60:
        return "только что"
    elif seconds < 3600:
        minutes = int(seconds // 60)
        return f"{minutes} мин назад"
    elif seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} ч назад"
    else:
        days = int(seconds // 86400)
        return f"{days} дн назад"

def status_emoji(status: str) -> str:
    mapping = {
        "GREEN": "🟢",
        "YELLOW": "🟡",
        "RED": "🔴",
        "GRAY": "⚪",
    }
    return mapping.get(status, "⚪")
