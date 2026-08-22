from datetime import datetime, timezone

def ensure_utc(dt: datetime) -> datetime:
    """
    Приводит datetime к UTC. Если объект наивный (без tzinfo),
    добавляет timezone.utc. Если уже с tzinfo, конвертирует в UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
