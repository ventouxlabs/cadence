"""``.env.example`` against the settings model, and the redaction of every secret.

A key that exists in one and not the other is a silent misconfiguration: an operator copies the
example, fills it in, and the app ignores half of it. The two lists are therefore compared as
sets, not spot-checked.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cadence.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = REPO_ROOT / ".env.example"
GITIGNORE = REPO_ROOT / ".gitignore"

SECRET_KEYS = {"VITALFORGE_TOKEN", "OMNIROUTE_KEY"}


def _env_aliases() -> set[str]:
    """The environment variable name behind each settings field."""
    aliases: set[str] = set()
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        assert isinstance(alias, str), f"{name} has no plain string env alias"
        aliases.add(alias)
    return aliases


def _env_example_entries() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        entries[key.strip()] = value.strip()
    return entries


#: Keys `.env` carries for **Compose**, not for the app. `.env` has two readers: pydantic-settings
#: builds `Settings` from it, and `docker compose` interpolates `${...}` in the compose files from
#: the same file. `CADENCE_BIND_ADDR` is the host interface the port is published on
#: (`docker-compose.yml`), which the process inside the container cannot know and must not read.
#: It belongs in the template because an operator has to set it (D-205), so the parity rule below
#: allows exactly these, and nothing else, to appear without a `Settings` field.
COMPOSE_ONLY_KEYS = frozenset({"CADENCE_BIND_ADDR"})


def test_env_example_keys_equal_the_settings_env_aliases() -> None:
    """No drift in either direction, except the documented Compose-only keys."""
    documented = set(_env_example_entries())
    declared = _env_aliases()
    extra = documented - declared - COMPOSE_ONLY_KEYS
    assert extra == set(), "documented in .env.example but not read by Settings"
    assert declared - documented == set(), "read by Settings but missing from .env.example"


def test_every_compose_only_key_is_actually_used_by_compose() -> None:
    """A key exempted from the parity rule has to earn it, or the exemption is a hiding place."""
    compose = (ENV_EXAMPLE.parent / "docker-compose.yml").read_text()
    for key in COMPOSE_ONLY_KEYS:
        assert f"${{{key}" in compose, f"{key} is exempt from parity but no compose file interpolates it"


def test_env_aliases_are_upper_snake_case() -> None:
    for alias in _env_aliases():
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", alias), alias


def test_every_env_alias_is_unique() -> None:
    """Two fields sharing one variable would make one of them unsettable."""
    aliases = [field.validation_alias for field in Settings.model_fields.values()]
    assert len(aliases) == len(set(aliases))


@pytest.mark.parametrize("key", sorted(SECRET_KEYS))
def test_env_example_ships_blank_secrets(key: str) -> None:
    """PRP-00 section 10: the example carries the URLs and the model names, never a token."""
    entries = _env_example_entries()
    assert key in entries
    assert entries[key] == "", f"{key} in .env.example is not blank"


def test_env_example_contains_no_credential_shaped_value() -> None:
    text = ENV_EXAMPLE.read_text()
    assert not re.search(r"sk-[A-Za-z0-9]", text)
    assert not re.search(r"Bearer\s+[A-Za-z0-9]", text)


def test_dotenv_is_ignored_and_absent() -> None:
    """Asked of git rather than of the file's text.

    This matched the literal line `.env` until D-205 broadened the rule to `.env*` with a
    `!.env.example` negation — at which point the old assertion failed while the property it
    existed to protect was strictly stronger. `git check-ignore` answers the actual question,
    and keeps answering it through any future rewrite of the patterns.
    """
    import subprocess

    result = subprocess.run(["git", "check-ignore", "-q", ".env"], cwd=str(REPO_ROOT), check=False, capture_output=True)
    assert result.returncode == 0, ".env is not git-ignored"
    assert "!.env.example" in GITIGNORE.read_text(), "the negation that keeps .env.example tracked is gone"


@pytest.mark.parametrize("field_name", ["vitalforge_token", "omniroute_key"])
def test_secret_fields_are_secretstr(field_name: str, clean_env: None) -> None:
    from pydantic import SecretStr

    settings = Settings(_env_file=None)
    assert isinstance(getattr(settings, field_name), SecretStr)


def test_secrets_are_redacted_in_every_rendering(clean_env: None) -> None:
    """``repr``, ``str``, f-strings, ``model_dump(mode="json")`` and the field's own repr."""
    token = "tok-do-not-log-1234567890"
    settings = Settings(VITALFORGE_TOKEN=token, OMNIROUTE_KEY=token, _env_file=None)
    renderings = [
        repr(settings),
        str(settings),
        f"{settings}",
        f"{settings!r}",
        str(settings.model_dump(mode="json")),
        repr(settings.vitalforge_token),
        str(settings.vitalforge_token),
        repr(settings.omniroute_key),
    ]
    for rendering in renderings:
        assert token not in rendering, rendering[:200]
    # The value is still reachable on purpose, through the one explicit accessor.
    assert settings.vitalforge_token.get_secret_value() == token


def test_a_secret_survives_model_copy_without_leaking(clean_env: None) -> None:
    """The immutability rule means settings get copied; a copy must not unwrap the secret."""
    token = "tok-do-not-log-1234567890"
    settings = Settings(VITALFORGE_TOKEN=token, _env_file=None)
    copied = settings.model_copy(update={"tz": "Europe/Paris"})
    assert token not in repr(copied)
    assert copied.vitalforge_token.get_secret_value() == token
    assert copied.tz == "Europe/Paris"
    assert settings.tz == "UTC"
