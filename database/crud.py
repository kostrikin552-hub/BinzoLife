from sqlalchemy import select, func, update, and_, or_, delete
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.ext.asyncio import AsyncSession
from database.models import (
    City, Station, FuelPrice, AvailabilityReport, User, Notification,
    FuelType, AvailabilityStatus, SourceType, UserAction, Payment, Review,
    UserAchievement, Referral, UserEconomy, CitySlug
)
from typing import Optional, List, Tuple
from datetime import datetime, timezone, timedelta
import random
import string

# -------- Вспомогательные ----------
def generate_referral_code(length=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

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
        await db.commit()
        await db.refresh(city)
    elif not city.is_active:
        city.is_active = True
        await db.commit()
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
    await db.commit()

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
    await db.commit()
    await db.refresh(station)
    return station

async def deactivate_station(db: AsyncSession, station_id: int) -> None:
    station = await get_station_by_id(db, station_id)
    if station:
        station.is_active = False
        await db.commit()

# -------- Цены ----------
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

async def save_price(db: AsyncSession, station_id: int, fuel_type: FuelType,
                     price: float, source: SourceType, confidence: float = 0.5,
                     recorded_at: datetime = None) -> FuelPrice:
    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc)
    entry = FuelPrice(
        station_id=station_id,
        fuel_type=fuel_type,
        price=price,
        source=source,
        confidence=confidence,
        recorded_at=recorded_at,
        is_fresh=True
    )
    db.add(entry)
    await db.commit()
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
            FuelPrice.recorded_at >= cutoff
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
            FuelPrice.recorded_at >= cutoff
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
            FuelPrice.recorded_at >= cutoff
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
            FuelPrice.recorded_at >= cutoff
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
    await db.commit()

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
            await db.commit()
            await db.refresh(report)
            return report, True
        else:
            await db.commit()
            await db.refresh(report)
            return report, False
    else:
        await db.commit()
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
    await db.commit()

# -------- Пользователи ----------
async def get_user(db: AsyncSession, telegram_id: int) -> Optional[User]:
    result = await db.execute(
        select(User)
        .where(User.telegram_id == telegram_id)
        .options(selectinload(User.city))
    )
    return result.scalar_one_or_none()

async def get_user_by_id(db: AsyncSession, user_id: int) -> Optional[User]:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()

async def get_user_by_referral_code(db: AsyncSession, code: str) -> Optional[User]:
    result = await db.execute(select(User).where(User.referral_code == code))
    return result.scalar_one_or_none()

async def create_user(db: AsyncSession, telegram_id: int, username: str = None) -> User:
    code = generate_referral_code()
    user = User(telegram_id=telegram_id, username=username, referral_code=code)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user

async def update_user(db: AsyncSession, user: User, **kwargs) -> User:
    for key, value in kwargs.items():
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    return user

async def add_reputation(db: AsyncSession, user: User, delta: int) -> User:
    user.reputation += delta
    await db.commit()
    await db.refresh(user)
    return user

# -------- Рефералы ----------
async def apply_referral(db: AsyncSession, new_user_id: int, referrer_code: str) -> bool:
    referrer = await get_user_by_referral_code(db, referrer_code)
    if not referrer or referrer.id == new_user_id:
        return False
    existing = await db.execute(select(Referral).where(Referral.referred_user_id == new_user_id))
    if existing.scalar_one_or_none():
        return False
    referral = Referral(referrer_id=referrer.id, referred_user_id=new_user_id)
    db.add(referral)
    await db.commit()
    await add_free_pro_days(db, referrer, 1)
    return True

async def get_referral_link(db: AsyncSession, user: User) -> str:
    if not user.referral_code:
        user.referral_code = generate_referral_code()
        await db.commit()
        await db.refresh(user)
    return f"https://t.me/BinzoLife_bot?start=ref_{user.referral_code}"

# -------- Достижения ----------
async def add_achievement(db: AsyncSession, user_id: int, ach_type: str, bonus_days: int = 0) -> UserAchievement:
    ach = UserAchievement(user_id=user_id, achievement_type=ach_type, bonus_days_granted=bonus_days)
    db.add(ach)
    await db.commit()
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
    await db.commit()
    await db.refresh(user)

async def check_and_award_achievements(db: AsyncSession, user_id: int):
    user = await get_user_by_id(db, user_id)
    if not user:
        return
    reports_count = await db.execute(select(func.count(AvailabilityReport.id)).where(AvailabilityReport.user_id == user_id))
    reports_count = reports_count.scalar()
    achievements_map = {
        10: ('reporter_10', 1),
        50: ('reporter_50', 3),
        100: ('reporter_100', 7),
    }
    for threshold, (ach_type, bonus_days) in achievements_map.items():
        if reports_count >= threshold:
            existing = await db.execute(select(UserAchievement).where(UserAchievement.user_id == user_id, UserAchievement.achievement_type == ach_type))
            if not existing.scalar_one_or_none():
                await add_achievement(db, user_id, ach_type, bonus_days)

async def get_user_achievements(db: AsyncSession, user_id: int) -> List[UserAchievement]:
    result = await db.execute(select(UserAchievement).where(UserAchievement.user_id == user_id))
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
    return [(row[0], row[1]) for row in result.all()]

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
    await db.commit()
    await db.refresh(notif)
    return notif

async def deactivate_notification(db: AsyncSession, notif_id: int) -> None:
    result = await db.execute(select(Notification).where(Notification.id == notif_id))
    notif = result.scalar_one_or_none()
    if notif:
        notif.active = False
        await db.commit()

async def update_notification_last_triggered(db: AsyncSession, notif_id: int) -> None:
    result = await db.execute(select(Notification).where(Notification.id == notif_id))
    notif = result.scalar_one_or_none()
    if notif:
        notif.last_triggered_at = datetime.now(timezone.utc)
        await db.commit()

# -------- Действия ----------
async def log_action(db: AsyncSession, user_id: int, action: str, station_id: Optional[int] = None):
    entry = UserAction(user_id=user_id, action=action, station_id=station_id)
    db.add(entry)
    await db.commit()

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
    await db.commit()
    await db.refresh(payment)
    return payment

async def activate_pro(db: AsyncSession, user: User, days: int = 30) -> User:
    now = datetime.now(timezone.utc)
    if user.pro_until and user.pro_until > now:
        user.pro_until = user.pro_until + timedelta(days=days)
    else:
        user.pro_until = now + timedelta(days=days)
    user.is_pro = True
    await db.commit()
    await db.refresh(user)
    return user

async def is_user_pro(db: AsyncSession, user: User) -> bool:
    if not user.is_pro:
        return False
    if user.pro_until is None:
        return True
    now = datetime.now(timezone.utc)
    if user.pro_until < now:
        user.is_pro = False
        await db.commit()
        return False
    return True

async def get_payment_by_telegram_charge_id(db: AsyncSession, charge_id: str) -> Optional[Payment]:
    result = await db.execute(select(Payment).where(Payment.telegram_payment_charge_id == charge_id))
    return result.scalar_one_or_none()

# -------- Отзывы ----------
async def create_review(db: AsyncSession, user_id: int, rating: int, comment: str = None) -> Review:
    review = Review(user_id=user_id, rating=rating, comment=comment)
    db.add(review)
    await db.commit()
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
    await db.commit()

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

# ========== НОВЫЕ ФУНКЦИИ ДЛЯ ПРОФИЛЯ ==========

async def get_user_search_count(db: AsyncSession, user_id: int) -> int:
    """Количество поисков (действий 'search_result')"""
    result = await db.execute(
        select(func.count(UserAction.id)).where(
            UserAction.user_id == user_id,
            UserAction.action == "search_result"
        )
    )
    return result.scalar() or 0

async def get_user_referrals_count(db: AsyncSession, user_id: int) -> int:
    """Количество пользователей, приглашённых данным пользователем"""
    result = await db.execute(
        select(func.count(Referral.id)).where(Referral.referrer_id == user_id)
    )
    return result.scalar() or 0

async def get_next_achievement_progress(db: AsyncSession, user_id: int):
    """
    Возвращает (название_достижения, текущее_значение, целевое_значение)
    для ближайшего недостигнутого достижения.
    """
    achievements_def = [
        ("Первый репорт", "reports", 1),
        ("Пригласил друга", "referrals", 1),
        ("Экономия 500 ₽", "saved", 500),
        ("Репортёр 10", "reports", 10),
        ("Репортёр 50", "reports", 50),
        ("Репортёр 100", "reports", 100),
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
        select(UserAchievement.achievement_type).where(UserAchievement.user_id == user_id)
    )
    achieved = {row[0] for row in achieved_types.all()}

    for name, key, target in achievements_def:
        ach_type = f"{key}_{target}"
        if ach_type in achieved:
            continue
        current = values.get(key, 0)
        if current < target:
            return (name, current, target)
    return None

async def get_missed_price_drops(db: AsyncSession, city_id: int, days: int = 7) -> int:
    """
    Возвращает количество раз, когда цена в городе была ниже средней за последние N дней.
    Для демонстрации возвращает случайное число от 0 до 5.
    На проде нужно реализовать полноценную логику на основе истории цен.
    """
    # Заглушка — позже заменим на реальный расчёт
    import random
    return random.randint(0, 5)

async def get_potential_saving(db: AsyncSession, user_id: int) -> float:
    """
    Потенциальная экономия, которую пользователь мог бы получить с PRO.
    На основе последних поисков и разницы цен между рекомендованной и средней.
    """
    # Заглушка — позже заменим на реальный расчёт
    return 0.0
