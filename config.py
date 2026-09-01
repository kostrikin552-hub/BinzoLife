import os
import sys
from typing import List, Optional
from dotenv import load_dotenv

# Загрузка переменных окружения из .env файла
load_dotenv()

class Settings:
    """Универсальный класс настроек проекта BinzoLife."""

    def __init__(self):
        # 1. ТОКЕН БОТА
        self.BOT_TOKEN: str = (
            os.getenv("BOT_TOKEN")
            or os.getenv("TELEGRAM_BOT_TOKEN")
            or os.getenv("TG_TOKEN")
            or os.getenv("TOKEN")
            or "8853126473:AAH203BM1dQWMtCxJzronMmiZmfQXU2W3Mg"
        )
        self.TOKEN: str = self.BOT_TOKEN
        self.TELEGRAM_BOT_TOKEN: str = self.BOT_TOKEN
        self.TG_TOKEN: str = self.BOT_TOKEN

        # 2. ПОДКЛЮЧЕНИЕ К БАЗЕ ДАННЫХ POSTGRESQL
        raw_db_url = (
            os.getenv("DATABASE_URL")
            or os.getenv("DB_URL")
            or os.getenv("POSTGRES_URL")
            or "postgresql+asyncpg://postgres:postgres@localhost:5432/binzolife"
        )

        # Автоматическая адаптация URL для SQLAlchemy + asyncpg
        if raw_db_url.startswith("postgres://"):
            self.DATABASE_URL: str = raw_db_url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif raw_db_url.startswith("postgresql://") and not raw_db_url.startswith("postgresql+asyncpg://"):
            self.DATABASE_URL: str = raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        else:
            self.DATABASE_URL: str = raw_db_url

        self.DB_URL: str = self.DATABASE_URL

        # 3. АДМИНИСТРАТОРЫ
        raw_admin_ids = os.getenv("ADMIN_IDS") or os.getenv("ADMINS") or os.getenv("ADMIN_ID") or ""
        self.ADMIN_IDS: List[int] = []
        if raw_admin_ids:
            for item in raw_admin_ids.replace(";", ",").replace(" ", ",").split(","):
                clean_item = item.strip()
                if clean_item.isdigit():
                    self.ADMIN_IDS.append(int(clean_item))
        self.ADMINS: List[int] = self.ADMIN_IDS

        # 4. ПЛАТЕЖНАЯ ИНФОРМАЦИЯ И ТАРИФЫ
        self.PAYMENT_PROVIDER_TOKEN: str = (
            os.getenv("PAYMENT_PROVIDER_TOKEN")
            or os.getenv("PAYMENTS_TOKEN")
            or os.getenv("PROVIDER_TOKEN")
            or ""
        )
        self.PAYMENTS_TOKEN: str = self.PAYMENT_PROVIDER_TOKEN

        self.PRICE_PRO_1_MONTH: int = int(os.getenv("PRICE_PRO_1_MONTH", 199))
        self.PRICE_PRO_3_MONTHS: int = int(os.getenv("PRICE_PRO_3_MONTHS", 499))
        self.PRICE_PRO_1_YEAR: int = int(os.getenv("PRICE_PRO_1_YEAR", 1490))

        # 5. ГЕОЛОКАЦИЯ И РАДИУС ПОИСКА
        self.DEFAULT_SEARCH_RADIUS_KM: float = float(os.getenv("DEFAULT_SEARCH_RADIUS_KM", 10.0))
        self.EMERGENCY_SEARCH_RADIUS_KM: float = float(os.getenv("EMERGENCY_SEARCH_RADIUS_KM", 5.0))
        self.MAX_SEARCH_RESULTS: int = int(os.getenv("MAX_SEARCH_RESULTS", 5))

        # 6. РЕЖИМ ОТЛАДКИ
        self.DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")

    def __getattr__(self, name: str) -> Any:
        # Безопасный возврат переменной окружения, если запрошен неизвестный атрибут
        return os.getenv(name, "")


# Создаем глобальный синглтон settings
settings = Settings()

# =====================================================================
# ПРЯМЫЕ ЭКСПОРТЫ ДЛЯ СОВМЕСТИМОСТИ
# (для модулей, импортирующих 'from config import BOT_TOKEN, DATABASE_URL')
# =====================================================================
BOT_TOKEN: str = settings.BOT_TOKEN
TOKEN: str = settings.TOKEN
TELEGRAM_BOT_TOKEN: str = settings.TELEGRAM_BOT_TOKEN
TG_TOKEN: str = settings.TG_TOKEN

DATABASE_URL: str = settings.DATABASE_URL
DB_URL: str = settings.DB_URL

ADMIN_IDS: List[int] = settings.ADMIN_IDS
ADMINS: List[int] = settings.ADMINS

PAYMENT_PROVIDER_TOKEN: str = settings.PAYMENT_PROVIDER_TOKEN
PAYMENTS_TOKEN: str = settings.PAYMENTS_TOKEN

PRICE_PRO_1_MONTH: int = settings.PRICE_PRO_1_MONTH
PRICE_PRO_3_MONTHS: int = settings.PRICE_PRO_3_MONTHS
PRICE_PRO_1_YEAR: int = settings.PRICE_PRO_1_YEAR

DEFAULT_SEARCH_RADIUS_KM: float = settings.DEFAULT_SEARCH_RADIUS_KM
EMERGENCY_SEARCH_RADIUS_KM: float = settings.EMERGENCY_SEARCH_RADIUS_KM
MAX_SEARCH_RESULTS: int = settings.MAX_SEARCH_RESULTS

DEBUG: bool = settings.DEBUG
