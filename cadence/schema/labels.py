"""Display names for the schema enums that reach a screen.

Here rather than in ``cadence/web/rendering.py`` because two packages need them: the templates,
and ``cadence/historique/`` when it names a day on the History list. Importing the web module
from a domain package dragged the whole Jinja environment along with it, which is a dependency
the query layer has no business having.

Only labels live here. Anything that formats a *value* (a load, a prescription) stays in
``rendering.py``, because that is presentation and this is vocabulary.
"""

from __future__ import annotations

from cadence.schema.enums import DayType

DAY_TYPE_LABELS: dict[str, str] = {
    DayType.UPPER_A.value: "Upper A",
    DayType.UPPER_B.value: "Upper B",
    DayType.LOWER_A.value: "Lower A",
    DayType.LOWER_FULL_B.value: "Lower / Full B",
    DayType.MOBILITY_CARRY.value: "Mobility & carry",
    DayType.ASSESSMENT.value: "Assessment day",
}

# A finished session whose planned row is gone has no day type left to name. It is still a
# session the user did, so it is listed under a plain word rather than under a blank.
ORPHAN_DAY_NAME = "Session"


def day_label(day_type: str | None) -> str:
    """The screen name of a ``DayType``, or a readable fallback for anything else."""
    if not day_type:
        return ORPHAN_DAY_NAME
    return DAY_TYPE_LABELS.get(day_type, day_type.replace("_", " ").capitalize())
