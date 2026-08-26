# database/models.py – ПОЛНЫЙ ФАЙЛ (с добавленным ON DELETE CASCADE)

from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, DateTime, Boolean, ForeignKey,
    Enum, Text, Index, func, Date
)
from sqlalchemy.orm import declarative_base, relationship
import enum

Base = declarative_base()

class FuelType(str, enum.Enum):
    AI_92 = "AI-92"
    AI_95 = "AI-95"
    AI_98 = "AI-98"
    DT = "DT"

class AvailabilityStatus(str, enum.Enum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"
    GRAY = "GRAY"

class SourceType(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"
    OFFICIAL = "official"
    PARSER = "parser"

class City(Base):
    __tablename__ = "cities"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    region = Column(String(100))
    latitude = Column(Float)
    longitude = Column(Float)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    stations = relationship("Station", back_populates="city")
    city_slug = relationship("CitySlug", back_populates="city", uselist=False)

class CitySlug(Base):
    __tablename__ = "city_slugs"
    city_id = Column(Integer, ForeignKey("cities.id", ondelete="CASCADE"), primary_key=True)
    slug = Column(String(50), nullable=False, unique=True)
    parser_source = Column(String(50), default="fuelprice")
    is_active = Column(Boolean, default=True)
    city = relationship("City", back_populates="city_slug")

class Station(Base):
    __tablename__ = "stations"
    id = Column(Integer, primary_key=True)
    city_id = Column(Integer, ForeignKey("cities.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False)
    brand = Column(String(100))
    address = Column(String(300))
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    daily_views = Column(Integer, default=0)
    last_view_date = Column(Date, nullable=True)

    city = relationship("City", back_populates="stations")
    prices = relationship("FuelPrice", back_populates="station")
    availability = relationship("AvailabilityReport", back_populates="station")

class FuelPrice(Base):
    __tablename__ = "fuel_prices"
    id = Column(Integer, primary_key=True)
    station_id = Column(Integer, ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    fuel_type = Column(Enum(FuelType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    price = Column(Float, nullable=False)
    source = Column(Enum(SourceType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    confidence = Column(Float, nullable=False, default=0.5)
    recorded_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    is_fresh = Column(Boolean, default=True)

    station = relationship("Station", back_populates="prices")

    __table_args__ = (
        Index("idx_prices_station_fuel", "station_id", "fuel_type"),
    )

class AvailabilityReport(Base):
    __tablename__ = "availability_reports"
    id = Column(Integer, primary_key=True)
    station_id = Column(Integer, ForeignKey("stations.id", ondelete="CASCADE"), nullable=False)
    fuel_type = Column(Enum(FuelType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    status = Column(Enum(AvailabilityStatus, values_callable=lambda x: [e.value for e in x]), nullable=False)
    source = Column(Enum(SourceType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    confidence = Column(Float, nullable=False, default=0.5)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    is_fresh = Column(Boolean, default=True)

    station = relationship("Station", back_populates="availability")
    user = relationship("User", back_populates="reports")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(100))
    city_id = Column(Integer, ForeignKey("cities.id", ondelete="SET NULL"), nullable=True)
    default_fuel = Column(Enum(FuelType, values_callable=lambda x: [e.value for e in x]), nullable=False, default=FuelType.AI_95.value)
    tank_volume = Column(Float, nullable=False, default=50.0)
    reputation = Column(Integer, nullable=False, default=0)
    is_pro = Column(Boolean, nullable=False, default=False)
    pro_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    total_saved = Column(Float, default=0.0)
    referral_code = Column(String(20), unique=True, nullable=True)
    referred_by = Column(BigInteger, nullable=True)
    auto_renew = Column(Boolean, default=False)
    first_search_at = Column(DateTime(timezone=True), nullable=True)
    funnel_stage = Column(Integer, default=0)
    last_funnel_message_at = Column(DateTime(timezone=True), nullable=True)
    trial_used = Column(Boolean, default=False)
    trial_started = Column(DateTime(timezone=True), nullable=True)
    silent_hours_start = Column(Integer, nullable=True)
    silent_hours_end = Column(Integer, nullable=True)

    city = relationship("City")
    reports = relationship("AvailabilityReport", back_populates="user")
    notifications = relationship("Notification", back_populates="user")
    payments = relationship("Payment", back_populates="user")
    reviews = relationship("Review", back_populates="user")
    achievements = relationship("UserAchievement", back_populates="user")
    economies = relationship("UserEconomy", back_populates="user")

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    station_id = Column(Integer, ForeignKey("stations.id", ondelete="CASCADE"), nullable=True)
    fuel_type = Column(Enum(FuelType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    target_price = Column(Float, nullable=True)
    notify_on_availability = Column(Boolean, nullable=False, default=False)
    notify_on_low_price = Column(Boolean, nullable=False, default=False)
    radius_km = Column(Float, nullable=True)
    active = Column(Boolean, nullable=False, default=True)
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="notifications")
    station = relationship("Station")

class UserAction(Base):
    __tablename__ = "user_actions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    action = Column(String(50), nullable=False)
    station_id = Column(Integer, ForeignKey("stations.id", ondelete="CASCADE"), nullable=True)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    telegram_payment_charge_id = Column(String(100), unique=True, nullable=False)
    provider_payment_charge_id = Column(String(100), nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False, default="RUB")
    status = Column(String(20), nullable=False, default="pending")
    tariff = Column(String(20), nullable=False, default="pro_month")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    paid_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="payments")

class Review(Base):
    __tablename__ = "reviews"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    rating = Column(Integer, nullable=False)
    comment = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="reviews")

class UserAchievement(Base):
    __tablename__ = "user_achievements"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    achievement_type = Column(String(50), nullable=False)
    awarded_at = Column(DateTime(timezone=True), server_default=func.now())
    bonus_days_granted = Column(Integer, default=0)

    user = relationship("User", back_populates="achievements")

class Referral(Base):
    __tablename__ = "referrals"
    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    referred_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    is_rewarded = Column(Boolean, default=False)

    referrer = relationship("User", foreign_keys=[referrer_id])
    referred = relationship("User", foreign_keys=[referred_user_id])

class UserEconomy(Base):
    __tablename__ = "user_economies"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    station_id = Column(Integer, ForeignKey("stations.id", ondelete="CASCADE"), nullable=True)
    price_paid = Column(Float, nullable=False)
    city_avg_price = Column(Float, nullable=False)
    saved = Column(Float, nullable=False)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="economies")
    station = relationship("Station")
