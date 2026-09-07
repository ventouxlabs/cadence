"""The read-only facts Today needs, cheap enough to ask for on every request.

``GET /today`` makes no network call (architecture section 5) and it must not re-read
``library/`` either: parsing and cross-validating the seed library costs ~75 ms warm, which is a
quarter of the ten-second-per-exercise budget spent before a row is drawn. The bundle is immutable
once loaded, so one process-wide copy is safe (D-070).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from sqlmodel import Session, select

from cadence.bibliotheque.bundle import LibraryBundle
from cadence.bibliotheque.loader import LIBRARY_DIR, LibraryError, load_library
from cadence.profils.settings import DEFAULT_SETTINGS, ProgramSettings, settings_from_rows
from cadence.profils.tables import Profile, Setting
from cadence.programme.bands import age_band
from cadence.schema.enums import AgeBand
from cadence.schema.youth import YouthRuleSet

DEFAULT_DISPLAY_UNIT = "kg"

logger = logging.getLogger(__name__)


class BandRulesUnavailable(RuntimeError):
    """The youth rule table could not be read, so no youth load limit can be computed.

    A gate that cannot perform its check must not report a pass (D-030). Raised rather than
    returning ``None`` so a caller has to decide, and the decision is visible in the type.
    """


@lru_cache(maxsize=4)
def _cached_bundle(path: str) -> tuple[LibraryBundle | None, str | None]:
    """The library and, when it will not parse, why. Cached together so both survive the lru."""
    try:
        return load_library(Path(path)), None
    except LibraryError as exc:
        # Today still renders: the checklist comes from ``planned_session.rows_json``, which was
        # validated when the plan was built. Only the *limits* are missing, and the callers that
        # need one refuse instead of guessing.
        logger.error("the exercise library will not load from %s: %s", path, exc)
        return None, str(exc)


def library_bundle(path: Path | None = None) -> LibraryBundle | None:
    """The seed library, loaded once per process. ``None`` when it will not parse."""
    return _cached_bundle(str(path or LIBRARY_DIR))[0]


def library_error(path: Path | None = None) -> str | None:
    """Why the library did not load, or ``None`` when it did."""
    return _cached_bundle(str(path or LIBRARY_DIR))[1]


def reset_library_cache() -> None:
    """Forget the cached bundle. Test-support only."""
    _cached_bundle.cache_clear()


def band_rules(profile: Profile, bundle: LibraryBundle | None = None) -> YouthRuleSet | None:
    """The rule set this profile is judged against, or ``None`` if the table is unreadable."""
    active = bundle if bundle is not None else library_bundle()
    if active is None:
        return None
    return active.youth_rules.get(age_band(profile))


def require_band_rules(profile: Profile, rules: YouthRuleSet | None) -> YouthRuleSet | None:
    """``rules`` for anyone who needs no cap, or a refusal for a youth profile that has none.

    An adult carries no load cap by design (principles section 3.2 has no adult numbers), so
    ``None`` there is an answer. For a youth profile ``None`` is the *absence* of an answer, and
    the difference is the whole point: without it the cap silently became "whatever was already
    on the row", which is not a limit any rule set agreed to.
    """
    if profile.kind != "youth" or rules is not None:
        return rules
    raise BandRulesUnavailable(
        f"the youth rules for band {age_band(profile).value!r} are unavailable, "
        f"so this load cannot be checked against a limit"
    )


def profile_band(profile: Profile) -> AgeBand:
    return age_band(profile)


def load_settings(session: Session) -> ProgramSettings:
    """The household settings, or the shipped defaults when nothing is stored yet."""
    rows = session.exec(select(Setting)).all()
    if not rows:
        return DEFAULT_SETTINGS
    return settings_from_rows({row.key: row.value_json for row in rows})


def display_unit(settings: ProgramSettings) -> str:
    """PRP-03 owns ``display_unit``; read it if present, default ``kg`` (PRP-02 scope out)."""
    value = getattr(settings, "display_unit", None)
    return str(value) if value else DEFAULT_DISPLAY_UNIT


def setup_complete(settings: ProgramSettings) -> bool:
    """Whether ``GET /`` may go straight to Today.

    One fact, one answer: the stored setting, which only a successful ``POST /setup`` sets. D-072's
    interim "two profiles exist means set up" branch is gone now that PRP-03 serves ``/setup`` --
    it was there to stop the front door redirecting to a route that did not exist, and keeping it
    would have let a seeded install skip the one screen the son's age is asked on.
    """
    return settings.setup_complete
