"""Which profiles this household is currently showing (D-260).

``son_enabled`` is a **display** setting, not a data one. Everything the son owns stays in the
database when it is off; what changes is which tabs are offered, which screens will render, and
which profile-addressed reads the JSON API will answer. That distinction is the whole feature:
turning it back on has to restore his plan exactly, so nothing here may delete, rebuild or
rewrite anything (D-261).

One module because two layers need the same answer and a second copy of the rule is a second
thing to keep in step: ``cadence.web.solo`` reads it to build the tab strip and to redirect a
screen, ``cadence.api.gates`` reads it to refuse a read. The rule itself is pure - it takes the
settings it is asked about and returns a verdict, so it can be tested without a request.
"""

from __future__ import annotations

from cadence.profils.settings import ProgramSettings
from cadence.profils.tables import PROFILE_ME, PROFILE_SON
from cadence.seance.today import TOGETHER, TodayError, normalise_profile_key

# The tab strip as the app knows it, in the order it is rendered. The labels live here rather
# than in the Jinja environment so that the keys and the rule that filters them cannot drift.
ALL_PROFILE_TABS: tuple[tuple[str, str], ...] = ((PROFILE_ME, "Me"), (PROFILE_SON, "Son"), (TOGETHER, "Both"))

# Together goes with the son, not with the parent: "Both" of one person is not a tab, it is the
# same screen with a second empty column.
HIDDEN_WHEN_SOLO: frozenset[str] = frozenset({PROFILE_SON, TOGETHER})


def son_enabled(settings: ProgramSettings) -> bool:
    """Whether the household is currently training two people."""
    return bool(settings.son_enabled)


def visible_profile_tabs(settings: ProgramSettings) -> tuple[tuple[str, str], ...]:
    """The tabs this household gets: all three, or "Me" on its own."""
    if son_enabled(settings):
        return ALL_PROFILE_TABS
    return tuple(tab for tab in ALL_PROFILE_TABS if tab[0] not in HIDDEN_WHEN_SOLO)


def is_hidden(settings: ProgramSettings, raw_profile: str | None) -> bool:
    """Whether ``?profile=`` names somebody this household is not currently showing.

    An **unknown** key is not hidden. It is unknown, and the 404 every caller already gives it
    says so; answering "hidden" instead would report a household setting to somebody who asked
    about a profile that has never existed, and would route a typo to the parent's screen rather
    than to the error it deserves.
    """
    if son_enabled(settings):
        return False
    try:
        return normalise_profile_key(raw_profile) in HIDDEN_WHEN_SOLO
    except TodayError:
        return False


__all__ = [
    "ALL_PROFILE_TABS",
    "HIDDEN_WHEN_SOLO",
    "is_hidden",
    "son_enabled",
    "visible_profile_tabs",
]
