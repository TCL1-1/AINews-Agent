from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "每日 AI 新闻助手"
    app_env: str = "development"
    app_secret_key: str = "dev-only-change-before-production"
    database_url: str = "sqlite:///./data/ai_news.db"

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    agent_max_rounds: int = Field(default=15, ge=1, le=50)
    agent_max_tool_calls: int = Field(default=25, ge=1, le=100)
    agent_timeout_seconds: int = Field(default=300, ge=30, le=1800)

    default_timezone: str = "Asia/Shanghai"
    default_digest_hour: int = Field(default=8, ge=0, le=23)
    default_digest_minute: int = Field(default=0, ge=0, le=59)
    default_max_items: int = Field(default=10, ge=3, le=30)
    default_coverage_hours: int = Field(default=24, ge=6, le=168)

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_use_tls: bool = True

    @property
    def runtime_dir(self) -> Path:
        path = BASE_DIR / "runtime"
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()

