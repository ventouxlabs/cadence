"""The equipment whitelist.

The ``Equipment`` enum *is* the whitelist (PRP-00 section 4.2): membership is the check, so a
document naming "barbell" fails at parse time and there is no second list to keep in sync.
"""

from __future__ import annotations

from cadence.schema.enums import Equipment

EQUIPMENT_WHITELIST: frozenset[Equipment] = frozenset(Equipment)


def is_whitelisted(value: object) -> bool:
    """True when ``value`` is (or names) a whitelisted equipment member."""
    if isinstance(value, Equipment):
        return value in EQUIPMENT_WHITELIST
    if isinstance(value, str):
        return any(value == member.value for member in EQUIPMENT_WHITELIST)
    return False
