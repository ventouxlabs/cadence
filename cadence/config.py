"""Typed settings, read from the environment and ``.env``.

Secrets are ``SecretStr`` so a stray ``repr(settings)`` in a log line prints asterisks rather
than a token. Nothing here is ever rendered into a response.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The one value that means "answer from a fixture instead of the network".
MOCK_MODE = "mock"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    # Which deployment this is. The only setting that can *refuse* another one: a fake
    # integration is a development convenience and a production data-loss bug (D-139).
    env: Literal["dev", "test", "prod"] = Field(default="dev", validation_alias="CADENCE_ENV")
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

    # The in-process periodic drain and metrics refresh. Off in tests by default, because a
    # background task that wakes up mid-assertion writes to the same rows the test is reading;
    # a test that wants it opts in explicitly (D-140).
    periodic_sync: bool = Field(default=True, validation_alias="CADENCE_PERIODIC_SYNC")

    # Display only. The Garmin wall-clock conversion happens server-side in VitalForge with
    # VitalForge's own TZ; Cadence always sends UTC with an explicit offset.
    tz: str = Field(default="UTC", validation_alias="TZ")

    @model_validator(mode="after")
    def _no_fakes_in_production(self) -> Settings:
        """Refuse to start a production process wired to a fake integration.

        ``CADENCE_VITALFORGE_MODE=mock`` answers every write with 202 and never opens a socket,
        so a production ``.env`` carrying it would mark every session ``sent`` while VitalForge
        heard nothing and Garmin got nothing — with a Done screen saying "synced ✓" the whole
        time. Nothing downstream can detect that, because from Cadence's side it looks exactly
        like success. Refusing at startup is the only place it can still be caught.

        Written against every ``*_mode`` field rather than against ``vitalforge_mode`` by name,
        so PRP-08's OmniRoute mode is covered by existing on the model rather than by somebody
        remembering to add it here.
        """
        if self.env != "prod":
            return self
        faked = sorted(
            name
            for name in type(self).model_fields
            if name.endswith("_mode") and getattr(self, name, None) == MOCK_MODE
        )
        if faked:
            raise ValueError(
                f"CADENCE_ENV=prod refuses mock integrations: {', '.join(faked)} is set to "
                f"{MOCK_MODE!r}. A mocked write-back reports success without sending anything."
            )
        return self

    @property
    def periodic_sync_enabled(self) -> bool:
        """Whether this process runs the background drain and refresh."""
        return self.periodic_sync and self.env != "test"

    @property
    def vitalforge_configured(self) -> bool:
        """Whether a VitalForge token is present. Never exposes the token itself."""
        return bool(self.vitalforge_token.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings. Tests clear the cache or override the dependency; they never mutate."""
    return Settings()
