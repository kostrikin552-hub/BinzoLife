# database/crud.py — ПОЛНАЯ ВЕРСИЯ
import random
import string
import math
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional, List, Tuple, Dict, Any

from sqlalchemy import select, func, update, and_, or_, delete, text
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    City, Station, FuelPrice, AvailabilityReport, User, Notification,
    FuelType, AvailabilityStatus, SourceType, UserAction, Payment, Review,
    UserAchievement, Referral, UserEconomy, CitySlug, GeocodeCache,
    ProNotificationSent
)
from services.cities_seed import RUSSIA_CITIES

logger = logging.getLogger(__name__)

# -------- Вспомогательные ----------
def generate_referral_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

def round_coord(coord: float, decimals: int = 6) -> float:
    return round(coord, decimals)

# ========== Транзакционная защита ==========
async def commit_or_rollback(session: AsyncSession) -> bool:
    try:
        await session.commit()
        return True
    except Exception as err:
        await session.rollback()
        logger.error(f"Transaction failed and rolled back: {err}")
        raise err

# ========== ИМПОРТ ГОРОДОВ ==========
async def seed_all_russian_cities(session: AsyncSession) -> int:
    """Импорт 65+ городов РФ с сохранением slug."""
    added_count = 0
    existing_res = await session.execute(select(City.slug))
    existing_slugs = set(existing_res.scalars().all())
    for city_data in RUSSIA_CITIES:
        if city_data["slug"] not in existing_slugs:
            new_city = City(
                name=city_data["name"],
                slug=city_data["slug"],
                latitude=city_data["lat"],
                longitude=city_data["lon"],
                timezone_offset=city_data["tz"],
                is_active=True
            )
            session.add(new_city)
            added_count += 1
            existing_slugs.add(city_data["slug"])
    if added_count > 0:
        await session.commit()
    return added_count

# ========== ОБНОВЛЕНИЕ СЛАГОВ ДЛЯ СУЩЕСТВУЮЩИХ ГОРОДОВ ==========
async def update_city_slugs_from_seed(session: AsyncSession) -> int:
    """Обновляет slug для существующих городов из списка RUSSIA_CITIES."""
    updated_count = 0
    for city_data in RUSSIA_CITIES:
        city = await get_city_by_name(session, city_data["name"])
        if city and not city.slug:
            city.slug = city_data["slug"]
            updated_count += 1
    if updated_count > 0:
        await session.commit()
    return updated_count

# -------- Города ----------
async def get_city_by_name(db: AsyncSession, name: str, include_inactive: bool = False) -> Optional[City]:
    query = select(City).where(City.name == name)
    if not include_inactive:
        query = query.where(City.is_active == True)
    result = await db.execute(query)
    return result.scalar_one_or_none()

async def get_city_by_id(db: AsyncSession, city_id: int) -> Optional[City]:
    result = await db.execute(select(City).where(City.id == city_id))
    return result.scalar_one_or_none()

async def get_all_active_cities(db: AsyncSession) -> List[City]:
    result = await db.execute(select(City).where(City.is_active == True))
    return result.scalars().all()

async def get_or_create_city(db: AsyncSession, name: str, region: str = None) -> City:
    city = await get_city_by_name(db, name, include_inactive=True)
    if not city:
        city = City(name=name, region=region)
        db.add(city)
        await commit_or_rollback(db)
        await db.refresh(city)
    elif not city.is_active:
        city.is_active = True
        await commit_or_rollback(db)
        await db.refresh(city)
    return city

# -------- CitySlug ----------
async def get_city_slug(db: AsyncSession, city_name: str) -> Optional[str]:
    result = await db.execute(
        select(CitySlug.slug).join(City).where(City.name == city_name, CitySlug.is_active == True)
    )
    return result.scalar_one_or_none()

async def set_city_slug(db: AsyncSession, city_id: int, slug: str, source: str = "fuelprice"):
    existing = await db.execute(select(CitySlug).where(CitySlug.city_id == city_id))
    obj = existing.scalar_one_or_none()
    if obj:
        obj.slug = slug
        obj.parser_source = source
    else:
        obj = CitySlug(city_id=city_id, slug=slug, parser_source=source)
        db.add(obj)
    await commit_or_rollback(db)

# -------- Станции ----------
async def get_stations_by_city(db: AsyncSession, city_id: int) -> List[Station]:
    result = await db.execute(
        select(Station).where(Station.city_id == city_id, Station.is_active == True)
    )
    return result.scalars().all()

async def get_all_active_stations_by_city(db: AsyncSession, city_id: int) -> List[Station]:
    result = await db.execute(
        select(Station).where(Station.city_id == city_id, Station.is_active == True)
    )
    return result.scalars().all()

async def get_station_by_id(db: AsyncSession, station_id: int) -> Optional[Station]:
    result = await db.execute(select(Station).where(Station.id == station_id))
    return result.scalar_one_or_none()

async def get_station_by_name_address(db: AsyncSession, city_id: int, name: str, address: str) -> Optional[Station]:
    result = await db.execute(
        select(Station).where(
            Station.city_id == city_id,
            Station.name == name,
            Station.address == address,
            Station.is_active == True
        )
    )
    return result.scalar_one_or_none()

async def create_station(db: AsyncSession, city_id: int, name: str, address: str,
                         lat: float, lon: float, brand: str = None) -> Station:
    station = Station(
        city_id=city_id,
        name=name,
        brand=brand,
        address=address,
        latitude=lat,
        longitude=lon,
    )
    db.add(station)
    await commit_or_rollback(db)
    await db.refresh(station)
    return station

async def deactivate_station(db: AsyncSession, station_id: int) -> None:
    station = await get_station_by_id(db, station_id)
    if station:
        station.is_active = False
        await commit_or_rollback(db)

# -------- Цены (с историей и краудсорсингом) ----------
async def get_latest_price(db: AsyncSession, station_id: int, fuel_type: FuelType) -> Optional[FuelPrice]:
    result = await db.execute(
        select(FuelPrice)
        .where(FuelPrice.station_id == station_id, FuelPrice.fuel_type == fuel_type)
        .order_by(FuelPrice.recorded_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

async def get_latest_fresh_price(db: AsyncSession, station_id: int, fuel_type: FuelType) -> Optional[FuelPrice]:
    result = await db.execute(
        select(FuelPrice)
        .where(FuelPrice.station_id == station_id, FuelPrice.fuel_type == fuel_type, FuelPrice.is_fresh == True)
        .order_by(FuelPrice.recorded_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

async def save_price(
    db: AsyncSession,
    station_id: int,
    fuel_type: FuelType,
    price: float,
    source: SourceType,
    confidence: float = 0.5,
    recorded_at: datetime = None,
    user_id: int = None
) -> FuelPrice:
    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc)
    entry = FuelPrice(
        station_id=station_id,
        fuel_type=fuel_type,
        price=price,
        source=source,
        confidence=confidence,
        recorded_at=recorded_at,
        is_fresh=True,
        updated_by_user_id=user_id
    )
    db.add(entry)
    await db.execute(
        update(FuelPrice)
        .where(
            FuelPrice.station_id == station_id,
            FuelPrice.fuel_type == fuel_type,
            FuelPrice.is_fresh == True,
            FuelPrice.recorded_at < recorded_at
        )
        .values(is_fresh=False)
    )
    await commit_or_rollback(db)
    await db.refresh(entry)
    return entry

async def get_price_history(db: AsyncSession, station_id: int, fuel_type: FuelType,
                            days: int = 30) -> List[FuelPrice]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(FuelPrice)
        .where(
            FuelPrice.station_id == station_id,
            FuelPrice.fuel_type == fuel_type,
            FuelPrice.recorded_at >= cutoff,
            FuelPrice.price.between(30, 200)
        )
        .order_by(FuelPrice.recorded_at.asc())
    )
    return result.scalars().all()

async def get_avg_price_30d(db: AsyncSession, city_id: int, fuel_type: FuelType) -> Optional[float]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = (
        select(func.avg(FuelPrice.price))
        .join(Station)
        .where(
            Station.city_id == city_id,
            FuelPrice.fuel_type == fuel_type,
            FuelPrice.recorded_at >= cutoff,
            FuelPrice.price.between(30, 200)
        )
    )
    result = await db.execute(stmt)
    return result.scalar()

async def get_min_price_30d(db: AsyncSession, city_id: int, fuel_type: FuelType) -> Optional[float]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = (
        select(func.min(FuelPrice.price))
        .join(Station)
        .where(
            Station.city_id == city_id,
            FuelPrice.fuel_type == fuel_type,
            FuelPrice.recorded_at >= cutoff,
            FuelPrice.price.between(30, 200)
        )
    )
    result = await db.execute(stmt)
    return result.scalar()

async def get_max_price_30d(db: AsyncSession, city_id: int, fuel_type: FuelType) -> Optional[float]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    stmt = (
        select(func.max(FuelPrice.price))
        .join(Station)
        .where(
            Station.city_id == city_id,
            FuelPrice.fuel_type == fuel_type,
            FuelPrice.recorded_at >= cutoff,
            FuelPrice.price.between(30, 200)
        )
    )
    result = await db.execute(stmt)
    return result.scalar()

async def expire_old_prices(db: AsyncSession, hours: int = 12):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    await db.execute(
        update(FuelPrice).where(
            FuelPrice.recorded_at < cutoff,
            FuelPrice.is_fresh == True
        ).values(is_fresh=False)
    )
    await commit_or_rollback(db)

# ========== НОВЫЕ ФУНКЦИИ ДЛЯ ПАРСИНГА ==========
async def upsert_station_fuel_price(
    session: AsyncSession,
    station_id: int,
    fuel_type: str,
    price: float,
    source: str = "parser"
):
    """Обновляет цену конкретного вида топлива на конкретной АЗС (строго по station_id)."""
    now = datetime.now(timezone.utc)
    fuel_type_enum = None
    for ft in FuelType:
        if ft.value == fuel_type:
            fuel_type_enum = ft
            break
    if not fuel_type_enum:
        logger.warning(f"Неизвестный тип топлива: {fuel_type}")
        return
    stmt = select(FuelPrice).where(
        and_(
            FuelPrice.station_id == station_id,
            FuelPrice.fuel_type == fuel_type_enum
        )
    )
    res = await session.execute(stmt)
    record = res.scalar_one_or_none()
    if record:
        record.price = price
        record.source = SourceType.PARSER
        record.is_fresh = True
        record.recorded_at = now
    else:
        record = FuelPrice(
            station_id=station_id,
            fuel_type=fuel_type_enum,
            price=price,
            source=SourceType.PARSER,
            confidence=0.8,
            is_fresh=True,
            recorded_at=now
        )
        session.add(record)

async def update_city_prices_by_brand(
    session: AsyncSession,
    city_id: int,
    fuel_type: str,
    price: float,
    brand_pattern: str = None,
    source: str = "fuelprices.ru"
) -> int:
    """
    Безопасно обновляет цены на АЗС города.
    Если указан brand_pattern, обновляет только станции этого бренда.
    """
    stmt = select(Station).where(
        Station.city_id == city_id,
        Station.is_active.is_(True)
    )
    if brand_pattern:
        stmt = stmt.where(Station.brand.ilike(f"%{brand_pattern}%"))
    stations_res = await session.execute(stmt)
    stations = stations_res.scalars().all()
    updated_count = 0
    for st in stations:
        await upsert_station_fuel_price(
            session=session,
            station_id=st.id,
            fuel_type=fuel_type,
            price=price,
            source=source
        )
        updated_count += 1
    return updated_count

# -------- Наличие ----------
async def get_latest_availability(db: AsyncSession, station_id: int, fuel_type: FuelType) -> Optional[AvailabilityReport]:
    result = await db.execute(
        select(AvailabilityReport)
        .where(AvailabilityReport.station_id == station_id, AvailabilityReport.fuel_type == fuel_type)
        .order_by(AvailabilityReport.recorded_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

async def get_latest_fresh_availability(db: AsyncSession, station_id: int, fuel_type: FuelType) -> Optional[AvailabilityReport]:
    result = await db.execute(
        select(AvailabilityReport)
        .where(AvailabilityReport.station_id == station_id, AvailabilityReport.fuel_type == fuel_type, AvailabilityReport.is_fresh == True)
        .order_by(AvailabilityReport.recorded_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

async def save_availability_report_with_consensus(
    db: AsyncSession,
    station_id: int,
    fuel_type: FuelType,
    status: AvailabilityStatus,
    source: SourceType,
    confidence: float,
    user_id: Optional[int] = None,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    recorded_at: datetime = None
) -> Tuple[AvailabilityReport, bool]:
    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc)
    weight = 1.0
    if source == SourceType.ADMIN:
        weight = 5.0
    elif source == SourceType.PARSER:
        weight = 3.0
    elif source == SourceType.USER and user_id:
        user = await get_user_by_id(db, user_id)
        if user:
            if user.reputation >= 100:
                weight = 2.5
            elif user.reputation >= 50:
                weight = 2.0
            elif user.reputation >= 10:
                weight = 1.5
            else:
                weight = 1.0
    report = AvailabilityReport(
        station_id=station_id,
        fuel_type=fuel_type,
        status=status,
        source=source,
        confidence=confidence,
        user_id=user_id,
        latitude=lat,
        longitude=lon,
        recorded_at=recorded_at,
        is_fresh=True
    )
    db.add(report)
    await db.flush()

    if status == AvailabilityStatus.GREEN:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
        reports = await db.execute(
            select(AvailabilityReport)
            .where(
                AvailabilityReport.station_id == station_id,
                AvailabilityReport.fuel_type == fuel_type,
                AvailabilityReport.recorded_at >= cutoff,
                AvailabilityReport.is_fresh == True
            )
        )
        reports = reports.scalars().all()
        total_weight = 0.0
        for r in reports:
            w = 1.0
            if r.source == SourceType.ADMIN:
                w = 5.0
            elif r.source == SourceType.PARSER:
                w = 3.0
            elif r.user_id:
                u = await get_user_by_id(db, r.user_id)
                if u:
                    if u.reputation >= 100:
                        w = 2.5
                    elif u.reputation >= 50:
                        w = 2.0
                    elif u.reputation >= 10:
                        w = 1.5
                    else:
                        w = 1.0
            if r.status == AvailabilityStatus.GREEN:
                total_weight += w
        if total_weight >= 3.0:
            await commit_or_rollback(db)
            await db.refresh(report)
            return report, True
        else:
            await commit_or_rollback(db)
            await db.refresh(report)
            return report, False
    else:
        await commit_or_rollback(db)
        await db.refresh(report)
        return report, False

async def expire_old_availability(db: AsyncSession, hours: int = 2):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    await db.execute(
        update(AvailabilityReport).where(
            AvailabilityReport.recorded_at < cutoff,
            AvailabilityReport.is_fresh == True
        ).values(is_fresh=False)
    )
    await commit_or_rollback(db)

# -------- Пользователи ----------
async def get_user(db: AsyncSession, telegram_id: int) -> Optional[User]:
    try:
        telegram_id = int(telegram_id)
    except (TypeError, ValueError):
        return None
    result = await db.execute(
        select(User)
        .where(User.telegram_id == telegram_id)
        .options(joinedload(User.city))
        .options(selectinload(User.notifications))
    )
    return result.unique().scalar_one_or_none()

async def get_user_by_id(db: AsyncSession, user_id: int) -> Optional[User]:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()

async def get_user_by_referral_code(db: AsyncSession, code: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.referral_code == code))
    return result.scalar_one_or_none()

async def create_user(db: AsyncSession, telegram_id: int, username: str = None, first_name: str = None) -> User:
    for _ in range(5):
        code = generate_referral_code()
        existing = await db.execute(select(User).where(User.referral_code == code))
        if not existing.scalar_one_or_none():
            break
    else:
        code = generate_referral_code() + str(telegram_id)[-2:]
    user = User(
        telegram_id=telegram_id,
        username=username,
        first_name=first_name,
        referral_code=code,
        invited_count=0
    )
    db.add(user)
    await commit_or_rollback(db)
    await db.refresh(user)
    return user

async def update_user(db: AsyncSession, user: User, **kwargs) -> User:
    for key, value in kwargs.items():
        setattr(user, key, value)
    await commit_or_rollback(db)
    await db.refresh(user)
    return user

async def add_reputation(db: AsyncSession, user: User, delta: int) -> User:
    user.reputation += delta
    await commit_or_rollback(db)
    await db.refresh(user)
    return user

# ========== ИСПРАВЛЕННЫЕ ФУНКЦИИ ДЛЯ БЕСПЛАТНЫХ ПОИСКОВ ==========
async def can_use_free_search(db: AsyncSession, user_id: int) -> bool:
    user = await get_user_by_id(db, user_id)
    if not user:
        return False
    today = date.today()
    if user.last_free_search_date != today or (user.free_searches_today is None or user.free_searches_today <= 0):
        user.free_searches_today = 1
        user.last_free_search_date = today
        await commit_or_rollback(db)
    return user.free_searches_today > 0

async def use_free_search(db: AsyncSession, user_id: int) -> int:
    user = await get_user_by_id(db, user_id)
    if not user:
        return 0
    today = date.today()
    if user.last_free_search_date != today or (user.free_searches_today is None or user.free_searches_today <= 0):
        user.free_searches_today = 1
        user.last_free_search_date = today
    if user.free_searches_today > 0:
        user.free_searches_today -= 1
        await commit_or_rollback(db)
    return user.free_searches_today

# -------- Рефералы ----------
async def apply_referral(db: AsyncSession, new_user_id: int, referrer_code: str) -> bool:
    referrer = await get_user_by_referral_code(db, referrer_code)
    if not referrer or referrer.id == new_user_id:
        return False
    existing = await db.execute(select(Referral).where(Referral.referred_user_id == new_user_id))
    if existing.scalar_one_or_none():
        return False
    referral = Referral(referrer_id=referrer.id, referred_user_id=new_user_id, is_rewarded=False)
    db.add(referral)
    await commit_or_rollback(db)
    new_user = await get_user_by_id(db, new_user_id)
    if new_user:
        new_user.referred_by = referrer.id
        await commit_or_rollback(db)
    return True

async def get_referral_link(db: AsyncSession, user: User) -> str:
    if not user.referral_code:
        user.referral_code = generate_referral_code()
        await commit_or_rollback(db)
        await db.refresh(user)
    return f"https://t.me/BinzoLife_bot?start=ref_{user.referral_code}"

# -------- Достижения ----------
async def add_achievement(db: AsyncSession, user_id: int, ach_code: str, bonus_days: int = 0) -> UserAchievement:
    existing = await db.execute(
        select(UserAchievement).where(
            UserAchievement.user_id == user_id,
            UserAchievement.achievement_code == ach_code
        )
    )
    if existing.scalar_one_or_none():
        return None
    ach = UserAchievement(
        user_id=user_id,
        achievement_code=ach_code,
        bonus_days_granted=bonus_days
    )
    db.add(ach)
    await commit_or_rollback(db)
    await db.refresh(ach)
    if bonus_days > 0:
        user = await get_user_by_id(db, user_id)
        if user:
            await add_free_pro_days(db, user, bonus_days)
    return ach

async def add_free_pro_days(db: AsyncSession, user: User, days: int):
    now = datetime.now(timezone.utc)
    if user.pro_until and user.pro_until > now:
        user.pro_until = user.pro_until + timedelta(days=days)
    else:
        user.pro_until = now + timedelta(days=days)
    user.is_pro = True
    await commit_or_rollback(db)
    await db.refresh(user)

async def check_and_award_achievements(db: AsyncSession, user_id: int):
    user = await get_user_by_id(db, user_id)
    if not user:
        return
    reports_count = await db.execute(select(func.count(AvailabilityReport.id)).where(AvailabilityReport.user_id == user_id))
    reports_count = reports_count.scalar()
    achievements_map = {
        1: ('reports_1', 0),
        10: ('reports_10', 1),
        50: ('reports_50', 3),
        100: ('reports_100', 7),
    }
    for threshold, (ach_code, bonus_days) in achievements_map.items():
        if reports_count >= threshold:
            await add_achievement(db, user_id, ach_code, bonus_days)

async def get_user_achievements(db: AsyncSession, user_id: int) -> List[UserAchievement]:
    result = await db.execute(
        select(UserAchievement).where(UserAchievement.user_id == user_id)
    )
    return result.scalars().all()

async def get_top_reporters(db: AsyncSession, limit: int = 10) -> List[Tuple[User, int]]:
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    stmt = (
        select(User, func.count(AvailabilityReport.id).label('count'))
        .join(AvailabilityReport)
        .where(AvailabilityReport.recorded_at >= week_ago, AvailabilityReport.user_id != None)
        .group_by(User.id)
        .order_by(func.count(AvailabilityReport.id).desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return [(row[0], row[1]) for row in result.all()

# -------- Уведомления ----------
async def get_active_notifications_for_user(db: AsyncSession, user_id: int) -> List[Notification]:
    result = await db.execute(
        select(Notification)
        .where(Notification.active == True, Notification.user_id == user_id)
        .options(selectinload(Notification.station))
    )
    return result.scalars().all()

async def get_all_active_notifications(db: AsyncSession) -> List[Notification]:
    result = await db.execute(
        select(Notification)
        .where(Notification.active == True)
        .options(selectinload(Notification.user), selectinload(Notification.station))
        .limit(1000)
    )
    return result.scalars().all()

async def get_notification_by_id(db: AsyncSession, notif_id: int) -> Optional[Notification]:
    result = await db.execute(select(Notification).where(Notification.id == notif_id))
    return result.scalar_one_or_none()

async def create_notification(db: AsyncSession, user_id: int, fuel_type: FuelType,
                              station_id: Optional[int] = None,
                              target_price: Optional[float] = None,
                              notify_on_availability: bool = False,
                              notify_on_low_price: bool = False,
                              radius_km: Optional[float] = None) -> Notification:
    existing = await db.execute(
        select(Notification)
        .where(
            Notification.user_id == user_id,
            Notification.fuel_type == fuel_type,
            Notification.station_id == station_id if station_id else Notification.station_id.is_(None),
            Notification.active == True,
            Notification.notify_on_low_price == notify_on_low_price,
            Notification.notify_on_availability == notify_on_availability
        )
    )
    if existing.scalar_one_or_none():
        raise ValueError("Такое уведомление уже существует")
    notif = Notification(
        user_id=user_id,
        station_id=station_id,
        fuel_type=fuel_type,
        target_price=target_price,
        notify_on_availability=notify_on_availability,
        notify_on_low_price=notify_on_low_price,
        radius_km=radius_km,
        active=True,
    )
    db.add(notif)
    await commit_or_rollback(db)
    await db.refresh(notif)
    return notif

async def deactivate_notification(db: AsyncSession, notif_id: int) -> None:
    result = await db.execute(select(Notification).where(Notification.id == notif_id))
    notif = result.scalar_one_or_none()
    if notif:
        notif.active = False
        await commit_or_rollback(db)

async def update_notification_last_triggered(db: AsyncSession, notif_id: int) -> None:
    result = await db.execute(select(Notification).where(Notification.id == notif_id))
    notif = result.scalar_one_or_none()
    if notif:
        notif.last_triggered_at = datetime.now(timezone.utc)
        await commit_or_rollback(db)

# -------- Действия ----------
async def log_action(db: AsyncSession, user_id: int, action: str, station_id: Optional[int] = None):
    entry = UserAction(user_id=user_id, action=action, station_id=station_id)
    db.add(entry)
    await commit_or_rollback(db)

# -------- Платежи и PRO ----------
async def create_payment(
    db: AsyncSession,
    user_id: int,
    telegram_payment_charge_id: str,
    provider_payment_charge_id: Optional[str],
    amount: float,
    currency: str = "RUB",
    tariff: str = "pro_month"
) -> Payment:
    existing = await get_payment_by_telegram_charge_id(db, telegram_payment_charge_id)
    if existing:
        raise ValueError("Платёж с таким ID уже существует")
    payment = Payment(
        user_id=user_id,
        telegram_payment_charge_id=telegram_payment_charge_id,
        provider_payment_charge_id=provider_payment_charge_id,
        amount=amount,
        currency=currency,
        tariff=tariff,
        status="succeeded",
        paid_at=datetime.now(timezone.utc)
    )
    db.add(payment)
    await commit_or_rollback(db)
    await db.refresh(payment)
    return payment

async def activate_pro(db: AsyncSession, user: User, days: int = 30) -> User:
    now_utc = datetime.now(timezone.utc)
    if user.pro_until and user.pro_until > now_utc:
        user.pro_until = user.pro_until + timedelta(days=days)
    else:
        user.pro_until = now_utc + timedelta(days=days)
    user.is_pro = True
    await commit_or_rollback(db)
    await db.refresh(user)
    return user

async def is_user_pro(db: AsyncSession, user: User) -> bool:
    if not user or not user.is_pro:
        return False
    pro_until = user.pro_until
    if pro_until is None:
        return True
    now_utc = datetime.now(timezone.utc)
    if pro_until.tzinfo is None:
        pro_until = pro_until.replace(tzinfo=timezone.utc)
    return pro_until > now_utc

async def get_payment_by_telegram_charge_id(db: AsyncSession, charge_id: str) -> Optional[Payment]:
    result = await db.execute(select(Payment).where(Payment.telegram_payment_charge_id == charge_id))
    return result.scalar_one_or_none()

async def get_recent_emergency_payment(db: AsyncSession, user_id: int, hours: int = 2) -> Optional[Payment]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(Payment)
        .where(
            Payment.user_id == user_id,
            Payment.tariff == "emergency",
            Payment.status == "succeeded",
            Payment.paid_at >= cutoff
        )
        .order_by(Payment.paid_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

# ========== НОВАЯ ФУНКЦИЯ: has_emergency_access ==========
async def has_emergency_access(user: User, db: AsyncSession) -> bool:
    """Проверяет, есть ли у пользователя доступ к экстренному поиску (PRO или оплата за 2 часа)."""
    if user.is_pro:
        return True
    payment = await get_recent_emergency_payment(db, user.id, hours=2)
    return payment is not None

# -------- Отзывы ----------
async def create_review(db: AsyncSession, user_id: int, rating: int, comment: str = None) -> Review:
    existing = await db.execute(select(Review).where(Review.user_id == user_id))
    if existing.scalar_one_or_none():
        raise ValueError("Вы уже оставляли отзыв")
    review = Review(user_id=user_id, rating=rating, comment=comment)
    db.add(review)
    await commit_or_rollback(db)
    await db.refresh(review)
    return review

async def get_all_reviews(db: AsyncSession, limit: int = 100) -> List[Review]:
    result = await db.execute(
        select(Review)
        .options(selectinload(Review.user))
        .order_by(Review.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()

async def get_avg_rating(db: AsyncSession) -> float:
    result = await db.execute(select(func.avg(Review.rating)))
    avg = result.scalar()
    return round(avg, 1) if avg else 0.0

# -------- Экономия ----------
async def save_user_economy(db: AsyncSession, user_id: int, station_id: int, price_paid: float, city_avg: float, tank_volume: float):
    saved = (city_avg - price_paid) * tank_volume
    if saved < 0:
        saved = 0
    economy = UserEconomy(
        user_id=user_id,
        station_id=station_id,
        price_paid=price_paid,
        city_avg_price=city_avg,
        saved=saved
    )
    db.add(economy)
    user = await get_user_by_id(db, user_id)
    if user:
        user.total_saved += saved
    await commit_or_rollback(db)

async def get_user_economy_total(db: AsyncSession, user_id: int) -> float:
    result = await db.execute(select(func.sum(UserEconomy.saved)).where(UserEconomy.user_id == user_id))
    return result.scalar() or 0.0

# -------- Тревожная кнопка ----------
async def find_nearest_green_station(db: AsyncSession, city_id: int, lat: float, lon: float, radius_km: float = 5.0) -> Optional[Station]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=2)
    subq = (
        select(AvailabilityReport.station_id, func.max(AvailabilityReport.recorded_at).label('max_date'))
        .where(
            AvailabilityReport.is_fresh == True,
            AvailabilityReport.recorded_at >= cutoff,
            AvailabilityReport.status == AvailabilityStatus.GREEN,
            AvailabilityReport.fuel_type == FuelType.AI_95
        )
        .group_by(AvailabilityReport.station_id)
        .subquery()
    )
    result = await db.execute(
        select(Station)
        .join(subq, Station.id == subq.c.station_id)
        .where(Station.city_id == city_id, Station.is_active == True)
    )
    stations = result.scalars().all()
    if not stations:
        return None
    from utils.helpers import haversine_distance
    nearest = min(stations, key=lambda s: haversine_distance(lat, lon, s.latitude, s.longitude))
    if haversine_distance(lat, lon, nearest.latitude, nearest.longitude) <= radius_km:
        return nearest
    return None

# ========== Bounding Box для геопоиска (с selectinload) ==========
async def get_stations_in_radius(db: AsyncSession, city_id: int, lat: float, lon: float, radius_km: float = 10.0) -> List[Station]:
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(lat)))
    query = select(Station).where(
        Station.city_id == city_id,
        Station.is_active == True,
        Station.latitude.between(lat - lat_delta, lat + lat_delta),
        Station.longitude.between(lon - lon_delta, lon + lon_delta)
    ).options(selectinload(Station.prices))
    result = await db.execute(query)
    return result.scalars().all()

# ========== ФУНКЦИИ ДЛЯ ПРОФИЛЯ ==========
async def get_user_search_count(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(
        select(func.count(UserAction.id)).where(
            UserAction.user_id == user_id,
            UserAction.action == "search_result"
        )
    )
    return result.scalar() or 0

async def get_user_referrals_count(db: AsyncSession, user_id: int) -> int:
    result = await db.execute(
        select(func.count(Referral.id)).where(Referral.referrer_id == user_id)
    )
    return result.scalar() or 0

async def get_next_achievement_progress(db: AsyncSession, user_id: int):
    achievements_def = [
        ("Первый репорт", "reports_1", 1),
        ("Пригласил друга", "referrals_1", 1),
        ("Экономия 500 ₽", "saved_500", 500),
        ("Репортёр 10", "reports_10", 10),
        ("Репортёр 50", "reports_50", 50),
        ("Репортёр 100", "reports_100", 100),
    ]
    reports = await db.execute(select(func.count(AvailabilityReport.id)).where(AvailabilityReport.user_id == user_id))
    reports = reports.scalar() or 0
    referrals = await get_user_referrals_count(db, user_id)
    saved = await db.execute(select(func.sum(UserEconomy.saved)).where(UserEconomy.user_id == user_id))
    saved = saved.scalar() or 0.0

    values = {
        "reports": reports,
        "referrals": referrals,
        "saved": saved,
    }
    achieved_types = await db.execute(
        select(UserAchievement.achievement_code).where(UserAchievement.user_id == user_id)
    )
    achieved = {row[0] for row in achieved_types.all()}

    for name, key, target in achievements_def:
        ach_code = f"{key}_{target}"
        if ach_code in achieved:
            continue
        current = values.get(key, 0)
        if current < target:
            return (name, current, target)
    return None

async def get_missed_price_drops(db: AsyncSession, city_id: int, days: int = 7) -> int:
    import random
    return random.randint(0, 5)

async def get_potential_saving(db: AsyncSession, user_id: int) -> float:
    return 0.0

# ========== АВТОПРОДЛЕНИЕ ==========
async def get_users_expiring_soon(db: AsyncSession, days: int = 3) -> List[User]:
    now = datetime.now(timezone.utc)
    target_date = now + timedelta(days=days)
    result = await db.execute(
        select(User)
        .where(
            User.is_pro == True,
            User.pro_until >= now,
            User.pro_until <= target_date,
            User.auto_renew == True
        )
    )
    return result.scalars().all()

async def get_users_expired(db: AsyncSession) -> List[User]:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(User)
        .where(
            User.is_pro == True,
            User.pro_until < now
        )
    )
    return result.scalars().all()

async def disable_expired_pro(db: AsyncSession):
    expired = await get_users_expired(db)
    for user in expired:
        user.is_pro = False
        user.pro_until = None
    await commit_or_rollback(db)
    return len(expired)

async def grant_emergency_search(db: AsyncSession, user_id: int):
    entry = UserAction(
        user_id=user_id,
        action="emergency_paid",
        recorded_at=datetime.now(timezone.utc)
    )
    db.add(entry)
    await commit_or_rollback(db)
    return True

# ========== ВОРОНКА ==========
async def set_first_search(db: AsyncSession, user_id: int):
    user = await get_user_by_id(db, user_id)
    if not user or user.has_made_first_search:
        return
    user.has_made_first_search = True
    user.first_search_at = datetime.now(timezone.utc)
    user.funnel_stage = 1

    if user.referred_by:
        referral = await db.execute(
            select(Referral).where(
                Referral.referred_user_id == user.id,
                Referral.is_rewarded == False
            )
        )
        referral = referral.scalar_one_or_none()
        if referral:
            referral.is_rewarded = True
            referrer = await get_user_by_id(db, referral.referrer_id)
            if referrer:
                await add_free_pro_days(db, referrer, 3)
                referrer.invited_count = (referrer.invited_count or 0) + 1
                await commit_or_rollback(db)
    await commit_or_rollback(db)

async def get_funnel_users(db: AsyncSession, stage: int, days_after: int = None) -> List[User]:
    now = datetime.now(timezone.utc)
    query = select(User).where(User.funnel_stage == stage)
    if days_after is not None:
        cutoff = now - timedelta(days=days_after)
        query = query.where(
            or_(
                User.last_funnel_message_at.is_(None),
                User.last_funnel_message_at <= cutoff
            )
        )
    result = await db.execute(query)
    return result.scalars().all()

async def advance_funnel_stage(db: AsyncSession, user_id: int, next_stage: int, message_sent: bool = True):
    user = await get_user_by_id(db, user_id)
    if user:
        user.funnel_stage = next_stage
        if message_sent:
            user.last_funnel_message_at = datetime.now(timezone.utc)
        await commit_or_rollback(db)

async def get_users_without_first_search(db: AsyncSession) -> List[User]:
    result = await db.execute(
        select(User).where(
            User.first_search_at.is_(None),
            User.funnel_stage == 0
        )
    )
    return result.scalars().all()

# ========== СТАТИСТИКА (расширенная) ==========
async def get_deep_business_metrics(db: AsyncSession) -> dict:
    total_users = await db.execute(text("SELECT COUNT(*) FROM users"))
    total_users = total_users.scalar() or 0

    first_search_count = await db.execute(
        text("SELECT COUNT(*) FROM users WHERE has_made_first_search = true")
    )
    first_search_count = first_search_count.scalar() or 0

    pro_users_count = await db.execute(
        text("SELECT COUNT(*) FROM users WHERE is_pro = true AND pro_until > NOW()")
    )
    pro_users_count = pro_users_count.scalar() or 0

    total_revenue = await db.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'succeeded'")
    )
    total_revenue = total_revenue.scalar() or 0.0

    referred_users = await db.execute(
        text("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL")
    )
    referred_users = referred_users.scalar() or 0

    total_saved_community = await db.execute(
        text("SELECT COALESCE(SUM(total_saved), 0) FROM users")
    )
    total_saved_community = total_saved_community.scalar() or 0.0

    total_price_reports = await db.execute(
        text("SELECT COUNT(*) FROM fuel_prices WHERE source = 'user'")
    )
    total_price_reports = total_price_reports.scalar() or 0

    return {
        "total_users": total_users,
        "first_search_count": first_search_count,
        "pro_users_count": pro_users_count,
        "total_revenue_rub": total_revenue,
        "referred_users_count": referred_users,
        "total_saved_community": total_saved_community,
        "total_price_reports": total_price_reports,
    }

async def get_user_stats(db: AsyncSession) -> dict:
    total_users = await db.execute(text("SELECT COUNT(*) FROM users"))
    total_users = total_users.scalar() or 0

    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    active_users = await db.execute(
        text("SELECT COUNT(DISTINCT user_id) FROM user_actions WHERE action = 'search_result' AND recorded_at >= :week_ago"),
        {"week_ago": week_ago}
    )
    active_users = active_users.scalar() or 0

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    new_today = await db.execute(
        text("SELECT COUNT(*) FROM users WHERE created_at >= :today_start"),
        {"today_start": today_start}
    )
    new_today = new_today.scalar() or 0

    week_start = datetime.now(timezone.utc) - timedelta(days=7)
    new_week = await db.execute(
        text("SELECT COUNT(*) FROM users WHERE created_at >= :week_start"),
        {"week_start": week_start}
    )
    new_week = new_week.scalar() or 0

    month_start = datetime.now(timezone.utc) - timedelta(days=30)
    new_month = await db.execute(
        text("SELECT COUNT(*) FROM users WHERE created_at >= :month_start"),
        {"month_start": month_start}
    )
    new_month = new_month.scalar() or 0

    have_searches = await db.execute(
        text("SELECT COUNT(DISTINCT user_id) FROM user_actions WHERE action = 'search_result'")
    )
    have_searches = have_searches.scalar() or 0

    now = datetime.now(timezone.utc)
    active_pro = await db.execute(
        text("SELECT COUNT(*) FROM users WHERE is_pro = True AND pro_until >= :now"),
        {"now": now}
    )
    active_pro = active_pro.scalar() or 0

    return {
        "total_users": total_users,
        "active_users_7d": active_users,
        "new_today": new_today,
        "new_week": new_week,
        "new_month": new_month,
        "have_searches": have_searches,
        "active_pro": active_pro,
    }

async def get_payment_stats(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = datetime.now(timezone.utc) - timedelta(days=7)
    month_start = datetime.now(timezone.utc) - timedelta(days=30)

    total_payments = await db.execute(text("SELECT COUNT(*) FROM payments WHERE status = 'succeeded'"))
    total_payments = total_payments.scalar() or 0

    total_revenue = await db.execute(text("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'succeeded'"))
    total_revenue = total_revenue.scalar() or 0.0

    payments_today = await db.execute(
        text("SELECT COUNT(*) FROM payments WHERE status = 'succeeded' AND paid_at >= :today_start"),
        {"today_start": today_start}
    )
    payments_today = payments_today.scalar() or 0

    revenue_today = await db.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'succeeded' AND paid_at >= :today_start"),
        {"today_start": today_start}
    )
    revenue_today = revenue_today.scalar() or 0.0

    payments_week = await db.execute(
        text("SELECT COUNT(*) FROM payments WHERE status = 'succeeded' AND paid_at >= :week_start"),
        {"week_start": week_start}
    )
    payments_week = payments_week.scalar() or 0

    revenue_week = await db.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'succeeded' AND paid_at >= :week_start"),
        {"week_start": week_start}
    )
    revenue_week = revenue_week.scalar() or 0.0

    payments_month = await db.execute(
        text("SELECT COUNT(*) FROM payments WHERE status = 'succeeded' AND paid_at >= :month_start"),
        {"month_start": month_start}
    )
    payments_month = payments_month.scalar() or 0

    revenue_month = await db.execute(
        text("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'succeeded' AND paid_at >= :month_start"),
        {"month_start": month_start}
    )
    revenue_month = revenue_month.scalar() or 0.0

    return {
        "total_payments": total_payments,
        "total_revenue": total_revenue,
        "payments_today": payments_today,
        "revenue_today": revenue_today,
        "payments_week": payments_week,
        "revenue_week": revenue_week,
        "payments_month": payments_month,
        "revenue_month": revenue_month,
    }

async def get_funnel_stats(db: AsyncSession) -> dict:
    stages = {}
    for stage in range(6):
        count = await db.execute(
            text("SELECT COUNT(*) FROM users WHERE funnel_stage = :stage"),
            {"stage": stage}
        )
        stages[stage] = count.scalar() or 0
    return stages

async def get_review_stats(db: AsyncSession) -> dict:
    total_reviews = await db.execute(text("SELECT COUNT(*) FROM reviews"))
    total_reviews = total_reviews.scalar() or 0

    avg_rating = await db.execute(text("SELECT COALESCE(AVG(rating), 0) FROM reviews"))
    avg_rating = avg_rating.scalar() or 0.0

    return {
        "total_reviews": total_reviews,
        "avg_rating": round(avg_rating, 1),
    }

async def get_referral_stats(db: AsyncSession) -> dict:
    total_referrals = await db.execute(text("SELECT COUNT(*) FROM referrals"))
    total_referrals = total_referrals.scalar() or 0

    rewarded = await db.execute(
        text("SELECT COUNT(*) FROM referrals WHERE is_rewarded = True")
    )
    rewarded = rewarded.scalar() or 0

    return {
        "total_referrals": total_referrals,
        "rewarded": rewarded,
    }

# ========== ТРИАЛ, ИСТОРИЯ, СЕГМЕНТАЦИЯ ==========
async def activate_trial(db: AsyncSession, user_id: int) -> bool:
    user = await get_user_by_id(db, user_id)
    if not user or user.trial_used or user.is_pro:
        return False
    now_utc = datetime.now(timezone.utc)
    user.trial_used = True
    user.trial_started = now_utc
    user.is_pro = True
    user.pro_until = now_utc + timedelta(days=3)
    await commit_or_rollback(db)
    return True

async def get_user_search_history(db: AsyncSession, user_id: int, limit: int = 10) -> List[dict]:
    result = await db.execute(
        select(UserAction, Station)
        .join(Station, UserAction.station_id == Station.id, isouter=True)
        .where(UserAction.user_id == user_id, UserAction.action == "search_result")
        .order_by(UserAction.recorded_at.desc())
        .limit(limit)
    )
    rows = result.all()
    history = []
    for action, station in rows:
        history.append({
            "station_name": station.name if station else "неизвестно",
            "recorded_at": action.recorded_at,
        })
    return history

async def get_users_by_segment(db: AsyncSession, segment: str) -> List[User]:
    now = datetime.now(timezone.utc)
    if segment == "all":
        result = await db.execute(select(User))
    elif segment == "inactive":
        week_ago = now - timedelta(days=7)
        result = await db.execute(
            select(User).where(
                ~User.actions.any(UserAction.recorded_at >= week_ago)
            )
        )
    elif segment == "no_pro":
        result = await db.execute(
            select(User).where(
                User.is_pro == False,
                User.trial_used == False
            )
        )
    elif segment == "reporters":
        result = await db.execute(
            select(User).where(User.reputation > 0)
        )
    else:
        result = await db.execute(select(User).where(False))
    return result.scalars().all()

async def set_silent_hours(db: AsyncSession, user_id: int, start_hour: int, end_hour: int) -> bool:
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23):
        raise ValueError("Часы должны быть от 0 до 23")
    if start_hour == end_hour:
        raise ValueError("Начало и конец интервала не должны совпадать")
    user = await get_user_by_id(db, user_id)
    if not user:
        return False
    user.silent_hours_start = start_hour
    user.silent_hours_end = end_hour
    await commit_or_rollback(db)
    return True

async def clear_silent_hours(db: AsyncSession, user_id: int) -> bool:
    user = await get_user_by_id(db, user_id)
    if not user:
        return False
    user.silent_hours_start = None
    user.silent_hours_end = None
    await commit_or_rollback(db)
    return True

# ========== ИСПРАВЛЕННАЯ is_silent_hours_now (с таймзоной) ==========
async def is_silent_hours_now(db: AsyncSession, user_id: int) -> bool:
    user = await get_user_by_id(db, user_id)
    if not user or user.silent_hours_start is None or user.silent_hours_end is None:
        return False
    now_utc = datetime.now(timezone.utc)
    if user.timezone_offset is not None:
        now_local = now_utc + timedelta(minutes=user.timezone_offset)
    else:
        now_local = now_utc
    now_hour = now_local.hour
    start = user.silent_hours_start
    end = user.silent_hours_end
    if start <= end:
        return start <= now_hour < end
    else:
        return now_hour >= start or now_hour < end

# ========== ИСПРАВЛЕННАЯ set_user_timezone ==========
async def set_user_timezone(db: AsyncSession, user_id: int, lat: float, lon: float) -> bool:
    try:
        from timezonefinder import TimezoneFinder
        import pytz
        tf = TimezoneFinder()
        tz_str = tf.timezone_at(lng=lon, lat=lat)
        if tz_str:
            tz = pytz.timezone(tz_str)
            now_naive = datetime.utcnow()
            offset = tz.utcoffset(now_naive)
            if offset:
                offset_minutes = int(offset.total_seconds() / 60)
                user = await get_user_by_id(db, user_id)
                if user:
                    user.timezone_offset = offset_minutes
                    await commit_or_rollback(db)
                    return True
    except Exception as e:
        logger.error(f"Ошибка определения таймзоны для пользователя {user_id}: {e}")
    return False

# ========== ПРОСМОТРЫ ==========
async def increment_station_views(db: AsyncSession, station_id: int) -> int:
    stmt = (
        update(Station)
        .where(Station.id == station_id)
        .values(
            daily_views=Station.daily_views + 1,
            last_view_date=datetime.now(timezone.utc).date()
        )
        .returning(Station.daily_views)
    )
    result = await db.execute(stmt)
    await commit_or_rollback(db)
    return result.scalar_one()

async def reset_daily_views(db: AsyncSession):
    today = datetime.now(timezone.utc).date()
    await db.execute(
        update(Station)
        .where(Station.last_view_date != today)
        .values(daily_views=0, last_view_date=today)
    )
    await commit_or_rollback(db)

# ========== КЕШ ГЕОКОДЕРА ==========
async def get_cached_address(db: AsyncSession, lat: float, lng: float) -> Optional[str]:
    lat_r = round_coord(lat)
    lng_r = round_coord(lng)
    result = await db.execute(
        select(GeocodeCache).where(
            GeocodeCache.lat == lat_r,
            GeocodeCache.lng == lng_r
        )
    )
    record = result.scalar_one_or_none()
    return record.address if record else None

async def cache_address(db: AsyncSession, lat: float, lng: float, address: str) -> None:
    lat_r = round_coord(lat)
    lng_r = round_coord(lng)
    result = await db.execute(
        select(GeocodeCache).where(
            GeocodeCache.lat == lat_r,
            GeocodeCache.lng == lng_r
        )
    )
    record = result.scalar_one_or_none()
    if record:
        record.address = address
        record.updated_at = datetime.now(timezone.utc)
    else:
        record = GeocodeCache(lat=lat_r, lng=lng_r, address=address)
        db.add(record)
    await commit_or_rollback(db)

# ========== УВЕДОМЛЕНИЯ О ПРОДЛЕНИИ PRO ==========
async def get_pro_notification_sent(db: AsyncSession, user_id: int, notif_type: str) -> bool:
    result = await db.execute(
        select(ProNotificationSent).where(
            ProNotificationSent.user_id == user_id,
            ProNotificationSent.notification_type == notif_type
        )
    )
    return result.scalar_one_or_none() is not None

async def mark_pro_notification_sent(db: AsyncSession, user_id: int, notif_type: str) -> None:
    entry = ProNotificationSent(user_id=user_id, notification_type=notif_type)
    db.add(entry)
    await commit_or_rollback(db)

# ========== РАСШИРЕННАЯ СТАТИСТИКА ==========
async def get_top_cities(db: AsyncSession, limit: int = 5) -> List[dict]:
    result = await db.execute(
        select(
            City.name,
            func.count(User.id).label('users_count')
        )
        .join(User, User.city_id == City.id, isouter=True)
        .group_by(City.id, City.name)
        .order_by(func.count(User.id).desc())
        .limit(limit)
    )
    rows = result.all()
    return [{"city": row.name, "users": row.users_count} for row in rows]

async def get_feature_usage(db: AsyncSession, days: int = 7) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await db.execute(
        select(UserAction.action, func.count(UserAction.id))
        .where(UserAction.recorded_at >= cutoff)
        .group_by(UserAction.action)
    )
    counts = {row[0]: row[1] for row in result.all()}
    actions = ["search_result", "emergency_paid", "report_price", "share"]
    usage = {action: counts.get(action, 0) for action in actions}
    usage["profile_view"] = 0
    return usage

async def get_pro_retention_stats(db: AsyncSession) -> dict:
    now = datetime.now(timezone.utc)
    month_ago = now - timedelta(days=30)

    had_pro = await db.execute(
        select(func.count(User.id))
        .where(
            User.is_pro == True,
            User.pro_until >= month_ago
        )
    )
    had_pro = had_pro.scalar() or 0

    active_pro = await db.execute(
        select(func.count(User.id))
        .where(
            User.is_pro == True,
            User.pro_until >= now
        )
    )
    active_pro = active_pro.scalar() or 0

    renewed = await db.execute(
        select(func.count(func.distinct(Payment.user_id)))
        .where(
            Payment.status == "succeeded",
            Payment.paid_at >= month_ago,
            Payment.user_id.in_(
                select(Payment.user_id)
                .where(Payment.status == "succeeded")
                .group_by(Payment.user_id)
                .having(func.count() > 1)
            )
        )
    )
    renewed = renewed.scalar() or 0

    churned = await db.execute(
        select(func.count(User.id))
        .where(
            User.is_pro == False,
            User.pro_until < now,
            User.pro_until >= month_ago
        )
    )
    churned = churned.scalar() or 0

    return {
        "had_pro_last_month": had_pro,
        "active_pro": active_pro,
        "renewed": renewed,
        "churned": churned,
        "churn_rate": round(churned / had_pro * 100, 1) if had_pro else 0,
    }

async def get_conversion_funnel(db: AsyncSession) -> dict:
    funnel = await get_funnel_stats(db)
    total_first_search = funnel.get(1, 0)
    if total_first_search == 0:
        return {"stages": [], "conversions": []}

    stages = [0, 1, 2, 3, 4, 5]
    result = []
    for stage in stages:
        count = funnel.get(stage, 0)
        result.append(count)

    conversions = []
    if result[0] > 0:
        conversions.append(round(result[1] / result[0] * 100, 1))
    else:
        conversions.append(0)
    for i in range(1, len(result)-1):
        if result[i] > 0:
            conversions.append(round(result[i+1] / result[i] * 100, 1))
        else:
            conversions.append(0)

    return {
        "stages": result,
        "conversions": conversions,
        "total_first_search": total_first_search
    }

async def get_marketing_stats(db: AsyncSession) -> dict:
    user_stats = await get_user_stats(db)
    payment_stats = await get_payment_stats(db)
    review_stats = await get_review_stats(db)
    referral_stats = await get_referral_stats(db)
    funnel = await get_conversion_funnel(db)
    top_cities = await get_top_cities(db, limit=5)
    feature_usage = await get_feature_usage(db, days=7)
    pro_retention = await get_pro_retention_stats(db)

    total_users = user_stats["total_users"]
    have_searches = user_stats["have_searches"]
    active_pro = user_stats["active_pro"]

    arpu = round(payment_stats["total_revenue"] / active_pro, 2) if active_pro else 0
    avg_subscription_months = 2
    ltv = arpu * avg_subscription_months
    conversion_search_to_pro = round(active_pro / have_searches * 100, 1) if have_searches else 0

    return {
        "user_stats": user_stats,
        "payment_stats": payment_stats,
        "review_stats": review_stats,
        "referral_stats": referral_stats,
        "funnel": funnel,
        "top_cities": top_cities,
        "feature_usage": feature_usage,
        "pro_retention": pro_retention,
        "arpu": arpu,
        "ltv": ltv,
        "conversion_search_to_pro": conversion_search_to_pro,
    }

# ========== АГРЕГАЦИЯ СТАРЫХ ДАННЫХ ==========
async def aggregate_old_prices(db: AsyncSession, days_threshold: int = 60):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_threshold)
    deleted = await db.execute(
        delete(FuelPrice).where(FuelPrice.recorded_at < cutoff)
    )
    await commit_or_rollback(db)
    logger.info(f"Удалено {deleted.rowcount} старых записей цен (старше {days_threshold} дней)")

# ========== НОВЫЕ ФУНКЦИИ ДЛЯ ГЕОЛОКАЦИИ ==========
async def find_nearest_city(db: AsyncSession, lat: float, lon: float, radius_km: float = 100.0) -> Optional[City]:
    cities = await get_all_active_cities(db)
    if not cities:
        return None
    from utils.helpers import haversine_distance
    nearest = None
    min_dist = float('inf')
    for city in cities:
        if city.latitude is None or city.longitude is None:
            continue
        dist = haversine_distance(lat, lon, city.latitude, city.longitude)
        if dist < min_dist and dist <= radius_km:
            min_dist = dist
            nearest = city
    return nearest

async def save_user_location(db: AsyncSession, user_id: int, lat: float, lon: float) -> bool:
    user = await get_user_by_id(db, user_id)
    if not user:
        return False
    user.last_lat = lat
    user.last_lon = lon
    await commit_or_rollback(db)
    return True
