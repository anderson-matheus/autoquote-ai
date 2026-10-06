"""Typed application settings, loaded from environment variables / `.env`."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Empty values in .env (e.g. `PII_ENCRYPTION_KEY=`) mean "not set", not "empty secret".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    log_file: str | None = "logs/execution.log"

    database_url: str = "postgresql+asyncpg://autoquote:autoquote@localhost:5432/autoquote"

    # --- Legacy quote service + resilience policy --------------------------------------
    quote_service_url: str = "http://localhost:8000"
    quote_attempt_timeout_s: float = Field(3.0, gt=0, description="Per-attempt timeout")
    quote_total_deadline_s: float = Field(12.0, gt=0, description="Budget for one quote")
    quote_max_attempts: int = Field(4, ge=1)
    quote_backoff_base_s: float = Field(0.4, ge=0)
    quote_backoff_max_s: float = Field(3.0, ge=0)
    breaker_failure_threshold: int = Field(5, ge=1)
    breaker_recovery_timeout_s: float = Field(30.0, gt=0)
    catalog_cache_ttl_s: float = Field(300.0, gt=0)

    # --- LLM (any OpenAI-compatible endpoint; Groq free tier by default) ------------------
    llm_api_key: SecretStr | None = None
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_timeout_s: float = Field(8.0, gt=0)

    # --- Conversation policy -------------------------------------------------------------
    max_stalled_turns: int = Field(3, ge=1)
    max_start_date_days_ahead: int = Field(90, ge=1)

    # --- Background re-quote of conversations handed off because the API was down --------
    requote_worker_enabled: bool = True
    requote_interval_s: float = Field(60.0, gt=0)
    requote_max_age_s: float = Field(1800.0, gt=0)

    # --- Security / PII ------------------------------------------------------------------
    pii_encryption_key: SecretStr | None = Field(
        None, description="Fernet key for contact address and CEP at rest"
    )
    contact_hash_secret: SecretStr = SecretStr("dev-only-contact-hash-secret")
    admin_api_key: SecretStr | None = None

    # --- WhatsApp Cloud API (simulated unless a token is configured) ----------------------
    whatsapp_verify_token: SecretStr = SecretStr("dev-verify-token")
    whatsapp_app_secret: SecretStr | None = None
    whatsapp_access_token: SecretStr | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_api_base_url: str = "https://graph.facebook.com/v20.0"

    @property
    def llm_enabled(self) -> bool:
        return self.llm_api_key is not None and bool(self.llm_api_key.get_secret_value())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
