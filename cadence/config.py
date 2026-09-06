"""Typed settings, read from the environment and ``.env``.

Secrets are ``SecretStr`` so a stray ``repr(settings)`` in a log line prints asterisks rather
than a token. Nothing here is ever rendered into a response.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    db_path: Path = Field(default=Path("data/cadence.db"), validation_alias="CADENCE_DB_PATH")
    # Binds all interfaces on purpose: the container is reached over Tailscale only (D-009).
    host: str = Field(default="0.0.0.0", validation_alias="CADENCE_HOST")
    port: int = Field(default=8000, validation_alias="CADENCE_PORT")
    vitalforge_mode: Literal["live", "mock"] = Field(default="live", validation_alias="CADENCE_VITALFORGE_MODE")

    vitalforge_weight_url: AnyHttpUrl = Field(
        default=AnyHttpUrl("https://weight.grepon.cc"), validation_alias="VITALFORGE_WEIGHT_URL"
    )
    vitalforge_dashboard_url: AnyHttpUrl = Field(
        default=AnyHttpUrl("https://health.grepon.cc"), validation_alias="VITALFORGE_DASHBOARD_URL"
    )
    vitalforge_token: SecretStr = Field(default=SecretStr(""), validation_alias="VITALFORGE_TOKEN")
    vitalforge_person_me: str = Field(default="", validation_alias="VITALFORGE_PERSON_ME")
    vitalforge_person_son: str = Field(default="", validation_alias="VITALFORGE_PERSON_SON")

    omniroute_url: AnyHttpUrl = Field(default=AnyHttpUrl("https://llm.grepon.cc/v1"), validation_alias="OMNIROUTE_URL")
    omniroute_key: SecretStr = Field(default=SecretStr(""), validation_alias="OMNIROUTE_KEY")
    omniroute_model_generate: str = Field(default="code-plan", validation_alias="OMNIROUTE_MODEL_GENERATE")
    omniroute_model_summary: str = Field(default="cheap-think", validation_alias="OMNIROUTE_MODEL_SUMMARY")

    # Display only. The Garmin wall-clock conversion happens server-side in VitalForge with
    # VitalForge's own TZ; Cadence always sends UTC with an explicit offset.
    tz: str = Field(default="UTC", validation_alias="TZ")

    @property
    def vitalforge_configured(self) -> bool:
        """Whether a VitalForge token is present. Never exposes the token itself."""
        return bool(self.vitalforge_token.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings. Tests clear the cache or override the dependency; they never mutate."""
    return Settings()
