"""Settings - acceptance tests 5-6."""

from __future__ import annotations

from pathlib import Path

from cadence.config import Settings


def test_settings_defaults(clean_env: None) -> None:
    settings = Settings(_env_file=None)
    assert settings.db_path == Path("data/cadence.db")
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.vitalforge_mode == "live"
    assert str(settings.vitalforge_weight_url).rstrip("/") == "https://weight.grepon.cc"
    assert str(settings.vitalforge_dashboard_url).rstrip("/") == "https://health.grepon.cc"
    assert str(settings.omniroute_url).rstrip("/") == "https://llm.grepon.cc/v1"
    assert settings.omniroute_model_generate == "code-plan"
    assert settings.omniroute_model_summary == "cheap-think"
    assert settings.vitalforge_person_me == ""
    assert settings.vitalforge_person_son == ""
    assert settings.tz == "UTC"
    assert settings.vitalforge_configured is False


def test_secrets_are_masked_in_repr(clean_env: None) -> None:
    settings = Settings(VITALFORGE_TOKEN="sekret", OMNIROUTE_KEY="sekret", _env_file=None)
    assert "sekret" not in repr(settings)
    assert "sekret" not in str(settings)
    assert "sekret" not in str(settings.model_dump(mode="json"))
    assert settings.vitalforge_token.get_secret_value() == "sekret"
