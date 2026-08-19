from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from database.models import (
    City, Station, FuelPrice, AvailabilityReport, User, Notification,
    FuelType, AvailabilityStatus, SourceType, UserAction, Payment, Review
)
from typing import Optional, List
from datetime import datetime, timezone, timedelta

# -------- Города --------
async def get_city_by_name(db: AsyncSession, name: str, include_inactive: bool = False) -> Optional[City]:
    query = select(City).where(City.name == name)
    if not include_inactive:
        query = query.where(City.is_active == True)
    result = await db.execute(query)
    return result.scalar_one_or_none()

async def get_city_by_id(db: AsyncSession, city_id: int) -> Optional[City]:
    result = await db.execute(select(City).where(City.id == city_id))
    return result.scalar_one_or_none()

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

# -------- Станции --------
async def get_stations_by_city(db: AsyncSession, city_id: int) -> List[Station]:
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

# -------- Цены --------
async def get_latest_price(db: AsyncSession, station_id: int, fuel_type: FuelType) -> Optional[FuelPrice]:
    result = await db.execute(
        select(FuelPrice)
        .where(FuelPrice.station_id == station_id, FuelPrice.fuel_type == fuel_type)
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
        recorded_at=recorded_at
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

# -------- Наличие --------
async def get_latest_availability(db: AsyncSession, station_id: int, fuel_type: FuelType) -> Optional[AvailabilityReport]:
    result = await db.execute(
        select(AvailabilityReport)
        .where(AvailabilityReport.station_id == station_id, AvailabilityReport.fuel_type == fuel_type)
        .order_by(AvailabilityReport.recorded_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()

async def save_availability_report(db: AsyncSession, station_id: int, fuel_type: FuelType,
                                   status: AvailabilityStatus, source: SourceType,
                                   confidence: float, user_id: Optional[int] = None,
                                   lat: Optional[float] = None, lon: Optional[float] = None,
                                   recorded_at: datetime = None) -> AvailabilityReport:
    if recorded_at is None:
        recorded_at = datetime.now(timezone.utc)
    report = AvailabilityReport(
        station_id=station_id,
        fuel_type=fuel_type,
        status=status,
        source=source,
        confidence=confidence,
        user_id=user_id,
        latitude=lat,
        longitude=lon,
        recorded_at=recorded_at
    )
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report

# -------- Пользователи --------
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

async def create_user(db: AsyncSession, telegram_id: int, username: str = None) -> User:
    user = User(telegram_id=telegram_id, username=username)
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

# -------- Уведомления --------
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
                              notify_on_low_price: bool = False) -> Notification:
    notif = Notification(
        user_id=user_id,
        station_id=station_id,
        fuel_type=fuel_type,
        target_price=target_price,
        notify_on_availability=notify_on_availability,
        notify_on_low_price=notify_on_low_price,
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

# -------- Действия --------
async def log_action(db: AsyncSession, user_id: int, action: str, station_id: Optional[int] = None):
    entry = UserAction(user_id=user_id, action=action, station_id=station_id)
    db.add(entry)
    await db.commit()

# -------- Платежи и PRO --------
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

# -------- Отзывы (новые функции) --------
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
