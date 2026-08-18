from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    BOT_TOKEN: str
    PROVIDER_TOKEN: str
    ADMIN_ID: str
    INTERNAL_TOKEN: str
    DATABASE_URL: str
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    @property
    def admin_ids(self) -> List[int]:
        return [int(x.strip()) for x in self.ADMIN_ID.split(",") if x.strip()]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

settings = Settings()
