import re
import zoneinfo
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    bot_token: str
    gemini_api_key: str
    database_url: str
    giga_key: str = ""  # base64-ключ GigaChat; пусто — GigaChat отключён
    giga_scope: str = "GIGACHAT_API_PERS"
    base_url: str = ""
    webhook_secret: str = "change-me"
    default_tz: str = "Europe/Moscow"
    polling: bool = False

    @property
    def webhook_path(self) -> str:
        return "/telegram/webhook"

    @property
    def webhook_url(self) -> str:
        return self.base_url.rstrip("/") + self.webhook_path

    @property
    def secret_token(self) -> str:
        """Telegram разрешает в secret token только A-Z, a-z, 0-9, _ и -."""
        clean = re.sub(r"[^A-Za-z0-9_-]", "", self.webhook_secret)
        return clean or "render-webhook-secret"


settings = Settings()


def get_tz(name: str | None = None) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(name or settings.default_tz)
    except Exception:
        return zoneinfo.ZoneInfo(settings.default_tz)
