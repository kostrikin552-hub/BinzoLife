from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, nullable=False)
    username = Column(String, nullable=True)
    city = Column(String, nullable=True)
    default_fuel = Column(String, default="АИ-95")
    tank_volume = Column(Float, nullable=True)
    reputation = Column(Integer, default=0)
    level = Column(String, default="Новичок")
    
    # PRO и триал
    pro_expires_at = Column(DateTime, nullable=True)
    trial_used = Column(Boolean, default=False)
    trial_expires_at = Column(DateTime, nullable=True)
    
    # Флаги уведомлений об окончании PRO
    pro_expiry_notified_3d = Column(Boolean, default=False)
    pro_expiry_notified_2d = Column(Boolean, default=False)
    pro_expiry_notified_1d = Column(Boolean, default=False)
    pro_expiry_notified_3h = Column(Boolean, default=False)
    pro_expiry_notified_1h = Column(Boolean, default=False)
    
    # Реферальная система
    ref_code = Column(String, unique=True, nullable=True)
    referred_by = Column(Integer, nullable=True)
    referral_bonus_days = Column(Integer, default=0)
    
    # Статистика
    total_searches = Column(Integer, default=0)
    total_saved = Column(Float, default=0.0)
    reports_count = Column(Integer, default=0)
    
    # Настройки
    quiet_hours_start = Column(Integer, default=23)
    quiet_hours_end = Column(Integer, default=7)
    
    # Воронка
    funnel_stage = Column(Integer, default=-1)
    first_search_at = Column(DateTime, nullable=True)
    
    created_at = Column(DateTime, default=datetime.now)

class Station(Base):
    __tablename__ = "stations"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    address = Column(String, nullable=False)
    city = Column(String, nullable=False)
    lat = Column(Float, nullable=True)
    lon = Column(Float, nullable=True)
    price = Column(Float, nullable=True)
    availability_status = Column(String, default="UNKNOWN")
    last_updated = Column(DateTime, default=datetime.now)
    data_source = Column(String, default="user")
    is_fresh = Column(Boolean, default=True)
    rating = Column(Float, nullable=True)
    reviews_count = Column(Integer, default=0)
    daily_views = Column(Integer, default=0)
    last_view_date = Column(DateTime, default=datetime.now)
    created_at = Column(DateTime, default=datetime.now)

class PriceReport(Base):
    __tablename__ = "price_reports"
    id = Column(Integer, primary_key=True)
    station_id = Column(Integer, ForeignKey("stations.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    price = Column(Float, nullable=False)
    status = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now)

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    station_id = Column(Integer, ForeignKey("stations.id"))
    type = Column(String)  # 'price' или 'availability'
    target_price = Column(Float, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)

class Referral(Base):
    __tablename__ = "referrals"
    id = Column(Integer, primary_key=True)
    referrer_id = Column(Integer, ForeignKey("users.id"))
    referred_id = Column(Integer, ForeignKey("users.id"))
    created_at = Column(DateTime, default=datetime.now)
