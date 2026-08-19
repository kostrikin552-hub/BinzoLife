from sqlalchemy import (
    Column, Integer, BigInteger, String, Float, DateTime, Boolean, ForeignKey,
    Enum, Text, Index, func
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

class Station(Base):
    __tablename__ = "stations"
    id = Column(Integer, primary_key=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=False)
    name = Column(String(200), nullable=False)
    brand = Column(String(100))
    address = Column(String(300))
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    city = relationship("City", back_populates="stations")
    prices = relationship("FuelPrice", back_populates="station")
    availability = relationship("AvailabilityReport", back_populates="station")

class FuelPrice(Base):
    __tablename__ = "fuel_prices"
    id = Column(Integer, primary_key=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False)
    fuel_type = Column(Enum(FuelType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    price = Column(Float, nullable=False)
    source = Column(Enum(SourceType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    confidence = Column(Float, nullable=False, default=0.5)
    recorded_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    station = relationship("Station", back_populates="prices")

    __table_args__ = (
        Index("idx_prices_station_fuel", "station_id", "fuel_type"),
    )

class AvailabilityReport(Base):
    __tablename__ = "availability_reports"
    id = Column(Integer, primary_key=True)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=False)
    fuel_type = Column(Enum(FuelType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    status = Column(Enum(AvailabilityStatus, values_callable=lambda x: [e.value for e in x]), nullable=False)
    source = Column(Enum(SourceType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    confidence = Column(Float, nullable=False, default=0.5)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    station = relationship("Station", back_populates="availability")
    user = relationship("User", back_populates="reports")

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)  # <-- ИСПРАВЛЕНО НА BIGINT
    username = Column(String(100))
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=True)
    default_fuel = Column(Enum(FuelType, values_callable=lambda x: [e.value for e in x]), nullable=False, default=FuelType.AI_95.value)
    tank_volume = Column(Float, nullable=False, default=50.0)
    reputation = Column(Integer, nullable=False, default=0)
    is_pro = Column(Boolean, nullable=False, default=False)
    pro_until = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    city = relationship("City")
    reports = relationship("AvailabilityReport", back_populates="user")
    notifications = relationship("Notification", back_populates="user")
    payments = relationship("Payment", back_populates="user")

class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=True)
    fuel_type = Column(Enum(FuelType, values_callable=lambda x: [e.value for e in x]), nullable=False)
    target_price = Column(Float, nullable=True)
    notify_on_availability = Column(Boolean, nullable=False, default=False)
    notify_on_low_price = Column(Boolean, nullable=False, default=False)
    active = Column(Boolean, nullable=False, default=True)
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="notifications")
    station = relationship("Station")

class UserAction(Base):
    __tablename__ = "user_actions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(50), nullable=False)
    station_id = Column(Integer, ForeignKey("stations.id"), nullable=True)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    telegram_payment_charge_id = Column(String(100), unique=True, nullable=False)
    provider_payment_charge_id = Column(String(100), nullable=True)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), nullable=False, default="RUB")
    status = Column(String(20), nullable=False, default="pending")
    tariff = Column(String(20), nullable=False, default="pro_month")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    paid_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="payments")
