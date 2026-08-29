from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict
import logging

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    BOT_TOKEN: str
    PROVIDER_TOKEN: str
    ADMIN_ID: str
    INTERNAL_TOKEN: str
    DATABASE_URL: str
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    YANDEX_GEOCODER_API_KEY: str = ""

    @property
    def admin_ids(self) -> List[int]:
        return [int(x.strip()) for x in self.ADMIN_ID.split(",") if x.strip()]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

settings = Settings()

# Предупреждения о заглушках (для администратора)
if settings.PROVIDER_TOKEN and settings.PROVIDER_TOKEN in ["ваш_провайдер_токен", "test", "provider"]:
    logger.warning("⚠️ PROVIDER_TOKEN выглядит как заглушка. Рублёвые платежи не будут работать.")
if settings.INTERNAL_TOKEN in ["ваш_секретный_токен", "secret", "token", "ваш_секретный_токен_для_cron"]:
    logger.warning("⚠️ INTERNAL_TOKEN слишком простой. Используйте генератор паролей.")
if settings.YANDEX_GEOCODER_API_KEY in ["ваш_ключ", ""]:
    logger.warning("⚠️ YANDEX_GEOCODER_API_KEY не задан. Геокодер будет использовать бесплатный Nominatim (с ограничениями).")
