"""``ProgramSettings`` - the household settings the program engine reads.

The values live in the ``setting`` table as JSON; this is the typed view of them. PRP-03 owns the
Setup and Settings screens that write them, PRP-01 ships the defaults ``make seed`` installs.

Named ``ProgramSettings`` rather than ``Settings`` so it never shadows ``cadence.config.Settings``,
which is a different thing entirely (environment and secrets).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import Field, StrictBool, StrictInt, field_validator

from cadence.schema.base import CadenceModel
from cadence.schema.enums import Equipment

SETTING_KEYS: tuple[str, ...] = (
    "equipment",
    "weights_available",
    "days_per_week",
    "session_minutes",
    "push_son_to_garmin",
    "setup_complete",
    "timers_default_on",
    "readiness_nudge_on",
)

DEFAULT_EQUIPMENT: tuple[Equipment, ...] = (
    Equipment.BODYWEIGHT,
    Equipment.DUMBBELLS,
    Equipment.KETTLEBELLS,
    Equipment.BENCH,
)

DEFAULT_WEIGHTS_AVAILABLE = "DB 5-52.5 lb adj step 2.5, KB 16/24 kg, BENCH adjustable"


class ProgramSettings(CadenceModel):
    """Frozen; ``with_changes`` returns a new one rather than writing through."""

    equipment: tuple[Equipment, ...] = Field(default=DEFAULT_EQUIPMENT, min_length=1)
    weights_available: str = Field(default=DEFAULT_WEIGHTS_AVAILABLE, max_length=500)
    days_per_week: StrictInt = Field(default=4, ge=2, le=6)
    session_minutes: StrictInt = Field(default=30, ge=15, le=45)
    push_son_to_garmin: StrictBool = False
    setup_complete: StrictBool = False
    timers_default_on: StrictBool = False
    readiness_nudge_on: StrictBool = True

    @field_validator("equipment")
    @classmethod
    def _dedupe(cls, value: tuple[Equipment, ...]) -> tuple[Equipment, ...]:
        seen: list[Equipment] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return tuple(seen)

    def with_changes(self, **fields: Any) -> ProgramSettings:
        """A new settings object with ``fields`` replaced, re-validated.

        ``model_copy(update=...)`` writes the values in untouched, so a caller passing
        ``equipment=["bodyweight"]`` would leave plain strings where the model promises
        ``Equipment`` members - and everything downstream that compares enums would quietly stop
        matching. Round-tripping through validation keeps the promise the type makes.
        """
        return ProgramSettings.model_validate({**self.model_dump(mode="json"), **fields})

    def as_rows(self) -> dict[str, str]:
        """The eight ``setting`` rows this object serialises to, as JSON text."""
        dumped = self.model_dump(mode="json")
        return {key: json.dumps(dumped[key]) for key in SETTING_KEYS}


DEFAULT_SETTINGS = ProgramSettings()


def settings_from_rows(rows: Iterable[tuple[str, str]] | Mapping[str, str]) -> ProgramSettings:
    """Rebuild settings from ``setting`` rows, ignoring keys this build does not know.

    A row whose JSON does not parse is dropped rather than raising: a corrupt single setting must
    not stop Today from rendering, and the default it falls back to is the one seed installed.
    """
    pairs = rows.items() if isinstance(rows, Mapping) else rows
    values: dict[str, Any] = {}
    for key, raw in pairs:
        if key not in SETTING_KEYS:
            continue
        try:
            values[key] = json.loads(raw)
        except (TypeError, ValueError):
            continue
    try:
        return ProgramSettings.model_validate(values)
    except ValueError:
        return DEFAULT_SETTINGS
