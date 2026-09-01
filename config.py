import os
import sys
from typing import List
from dotenv import load_dotenv

# Загружаем переменные из .env файла, если он есть
load_dotenv()

# =====================================================================
# 1. ТОКЕН БОТА TELEGRAM
# =====================================================================
# Проверяем все возможные варианты названий переменной окружения
BOT_TOKEN: str = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("TG_TOKEN")
    or os.getenv("TOKEN")
    or ""
)

if not BOT_TOKEN:
    print(
        "CRITICAL ERROR: Токен бота не найден в переменных окружения! "
        "Укажите BOT_TOKEN или TELEGRAM_BOT_TOKEN в настройках окружения (Environment) на Render.",
        file=sys.stderr
    )

# Алиасы токена для любых модулей
TOKEN: str = BOT_TOKEN
TELEGRAM_BOT_TOKEN: str = BOT_TOKEN
TG_TOKEN: str = BOT_TOKEN

# =====================================================================
# 2. БАЗА ДАННЫХ POSTGRESQL
# =====================================================================
# Поддержка стандартов DATABASE_URL (включая автоматическое приведение к asyncpg)
raw_db_url = (
    os.getenv("DATABASE_URL")
    or os.getenv("DB_URL")
    or os.getenv("POSTGRES_URL")
    or "postgresql+asyncpg://postgres:postgres@localhost:5432/binzolife"
)

# Render часто выдает postgres:// или postgresql:// — исправляем на asyncpg драйвер для SQLAlchemy
if raw_db_url.startswith("postgres://"):
    DATABASE_URL = raw_db_url.replace("postgres://", "postgresql+asyncpg://", 1)
elif raw_db_url.startswith("postgresql://") and not raw_db_url.startswith("postgresql+asyncpg://"):
    DATABASE_URL = raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    DATABASE_URL = raw_db_url

DB_URL: str = DATABASE_URL

# =====================================================================
# 3. АДМИНИСТРАТОРЫ И УПРАВЛЕНИЕ
# =====================================================================
raw_admin_ids = os.getenv("ADMIN_IDS") or os.getenv("ADMINS") or os.getenv("ADMIN_ID") or ""

ADMIN_IDS: List[int] = []
if raw_admin_ids:
    for item in raw_admin_ids.replace(";", ",").replace(" ", ",").split(","):
        clean_item = item.strip()
        if clean_item.isdigit():
            ADMIN_IDS.append(int(clean_item))

# Алиасы администраторов
ADMINS: List[int] = ADMIN_IDS

# =====================================================================
# 4. ПЛАТЕЖНЫЕ ДАННЫЕ И ТАРИФЫ
# =====================================================================
PAYMENT_PROVIDER_TOKEN: str = (
    os.getenv("PAYMENT_PROVIDER_TOKEN")
    or os.getenv("PAYMENTS_TOKEN")
    or os.getenv("PROVIDER_TOKEN")
    or ""
)
PAYMENTS_TOKEN: str = PAYMENT_PROVIDER_TOKEN

# Цены тарифов (в рублях)
PRICE_PRO_1_MONTH: int = int(os.getenv("PRICE_PRO_1_MONTH", 199))
PRICE_PRO_3_MONTHS: int = int(os.getenv("PRICE_PRO_3_MONTHS", 499))
PRICE_PRO_1_YEAR: int = int(os.getenv("PRICE_PRO_1_YEAR", 1490))

# =====================================================================
# 5. НАСТРОЙКИ СЕРВИСОВ И ГЕОЛОКАЦИИ
# =====================================================================
DEFAULT_SEARCH_RADIUS_KM: float = float(os.getenv("DEFAULT_SEARCH_RADIUS_KM", 10.0))
EMERGENCY_SEARCH_RADIUS_KM: float = float(os.getenv("EMERGENCY_SEARCH_RADIUS_KM", 5.0))
MAX_SEARCH_RESULTS: int = int(os.getenv("MAX_SEARCH_RESULTS", 5))

# Режим отладки
DEBUG: bool = os.getenv("DEBUG", "false").lower() in ("true", "1", "yes")
