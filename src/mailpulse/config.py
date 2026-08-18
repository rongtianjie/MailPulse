from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAILPULSE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "MailPulse"
    # Environment label used for production secret validation; it does not switch services.
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8080
    secret_key: str = "mailpulse-development-only-change-me"
    credential_key: str | None = None
    data_dir: Path = Path("var")
    database_url: str | None = None
    log_level: str = "INFO"
    log_rotation: str = "00:00"
    log_retention: str = "30 days"
    session_cookie: str = "mailpulse_session"
    remember_me_days: int = Field(default=30, ge=1, le=365)
    remember_password_days: int = Field(default=30, ge=1, le=365)
    session_https_only: bool = False
    default_admin_username: str = "admin"
    default_admin_password: str = Field(default="admin123", min_length=8)
    default_admin_display_name: str = "系统管理员"

    ai_base_url: str | None = None
    ai_api_key: str | None = None
    ai_model: str | None = None
    ai_primary_supports_image: bool = False
    ai_primary_supports_structured_output: bool = True
    ai_vision_base_url: str | None = None
    ai_vision_api_key: str | None = None
    ai_vision_model: str | None = None
    ai_vision_supports_structured_output: bool = True
    external_ai_allowed: bool = False
    ai_timeout_seconds: float = 90.0
    ai_max_output_tokens: int = 1800
    ai_max_input_chars: int = Field(default=120_000, ge=4_096)
    ai_max_retries: int = Field(default=2, ge=0, le=5)
    max_messages_per_report: int = Field(default=100, ge=1, le=1_000)
    job_log_retention_days: int = Field(default=90, ge=1, le=3650)
    job_log_retention_count: int = Field(default=100, ge=1, le=10_000)
    job_worker_batch_size: int = Field(default=4, ge=1, le=100)

    max_attachment_bytes: int = Field(default=20 * 1024 * 1024, ge=1024)
    max_attachments_per_message: int = Field(default=20, ge=1, le=100)
    max_image_assets_per_attachment: int = Field(default=20, ge=1, le=100)
    max_image_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_user_storage_bytes: int = Field(default=512 * 1024 * 1024, ge=1024)
    max_global_storage_bytes: int = Field(default=10 * 1024 * 1024 * 1024, ge=1024)

    @model_validator(mode="after")
    def validate_production_secrets(self):
        if self.environment.lower() in {"production", "prod"}:
            if self.secret_key == "mailpulse-development-only-change-me":
                raise ValueError("production 环境必须配置 MAILPULSE_SECRET_KEY")
            if not self.credential_key:
                raise ValueError("production 环境必须配置 MAILPULSE_CREDENTIAL_KEY")
        return self

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'mailpulse.sqlite3').resolve()}"

    @property
    def attachments_dir(self) -> Path:
        path = self.data_dir / "attachments"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def conversions_dir(self) -> Path:
        path = self.data_dir / "conversions"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def logs_dir(self) -> Path:
        path = self.data_dir / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
