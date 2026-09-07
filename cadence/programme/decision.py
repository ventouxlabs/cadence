"""The autoregulation decision table - principles section 5.4, in order, first match wins.

Written as an explicit ordered list of ``(rule_id, predicate, outcome)`` rather than a chain of
``if``/``elif``. Two rules can share an outcome and differ in meaning - R7 and R11 both hold, and
only the rule id says whether the session left rows unticked or simply had nothing to react to -
so a test that asserts the outcome alone cannot tell a reordering from a correct table (PRP-07
devil's-advocate risk 1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

Outcome = Literal["bump", "hold", "regress", "deload"]
Felt = Literal["easy", "right", "hard"]
Readiness = Literal["low", "ok", "high", "unknown"]
Completion = Literal["complete", "partial"]

DELOAD_WEEK = 4
BUMPABLE_READINESS: frozenset[str] = frozenset({"ok", "high", "unknown"})


@dataclass(frozen=True, slots=True)
class AutoregInput:
    """Principles section 5.3: everything the table is allowed to read.

    ``readiness`` defaults to ``unknown`` and never to ``low``: an absent ``metrics_cache`` means
    nothing is known about the person, and reading that as "low" would hold every session forever
    on a household that never connected VitalForge (risk 6).
    """

    all_rows_ticked: bool
    felt: Felt | None = None
    readiness: Readiness = "unknown"
    missed_sessions_7d: int = 0
    week_of_block: int = 1
    completion: Completion = "complete"

    @property
    def left_rows_unticked(self) -> bool:
        """A **full** session with rows left unticked.

        Section 5.4's footnote excludes a ``partial`` session from R7, and the PRP extends the
        same exclusion to R2 in as many words: "a partial session with ``felt == hard`` still
        reaches R5". R2 cannot be reached by a partial session for that sentence to be true, so
        both rules read this rather than ``all_rows_ticked`` alone (D-211).
        """
        return not self.all_rows_ticked and self.completion == "complete"


@dataclass(frozen=True, slots=True)
class Decision:
    """What to do, and which rule said so.

    The PRP's own signature returns a bare outcome while its prose asks for the rule id "alongside
    the outcome for logging", and acceptance tests 1 and 5 both assert the id. One return value
    carrying both settles the contradiction (D-212).
    """

    outcome: Outcome
    rule_id: str
    #: Whether the table would have bumped had week 4 not intervened (section 5.4's stored bump).
    earned_bump: bool = False


Predicate = Callable[[AutoregInput], bool]

# Principles section 5.4, verbatim and in order. Do not sort, do not group by outcome.
RULES: tuple[tuple[str, Predicate, Outcome], ...] = (
    ("R1", lambda i: i.week_of_block == DELOAD_WEEK, "deload"),
    ("R2", lambda i: i.felt == "hard" and i.left_rows_unticked, "regress"),
    ("R3", lambda i: i.missed_sessions_7d >= 2, "regress"),
    ("R4", lambda i: i.felt == "hard" and i.readiness == "low", "regress"),
    ("R5", lambda i: i.felt == "hard", "hold"),
    ("R6", lambda i: i.readiness == "low", "hold"),
    ("R7", lambda i: i.left_rows_unticked, "hold"),
    ("R8", lambda i: i.missed_sessions_7d == 1, "hold"),
    (
        "R9",
        lambda i: i.all_rows_ticked and i.felt == "easy" and i.readiness in BUMPABLE_READINESS,
        "bump",
    ),
    ("R10", lambda i: i.all_rows_ticked and i.felt == "right", "hold"),
    ("R11", lambda _: True, "hold"),
)

# R1 is the only absolute (P2). Everything after it decides what the week would otherwise have
# been, which is how a bump earned in week 4 survives as ``pending_bump``.
RULES_AFTER_DELOAD: tuple[tuple[str, Predicate, Outcome], ...] = RULES[1:]


def _first_match(rules: tuple[tuple[str, Predicate, Outcome], ...], inp: AutoregInput) -> tuple[str, Outcome]:
    for rule_id, predicate, outcome in rules:
        if predicate(inp):
            return rule_id, outcome
    # Unreachable: R11 has no condition. Kept so a future edit to the table cannot fall off it.
    return "R11", "hold"  # pragma: no cover


def decide(inp: AutoregInput) -> Decision:
    """The section 5.4 table. First match wins; the rule id comes back with the outcome.

    ``felt is None`` matches neither R2, R4 nor R5, because every one of them tests for ``hard``
    explicitly - a session finished without answering the prompt falls through to R6-R11.
    """
    rule_id, outcome = _first_match(RULES, inp)
    if outcome != "deload":
        return Decision(outcome=outcome, rule_id=rule_id, earned_bump=outcome == "bump")
    _, would_be = _first_match(RULES_AFTER_DELOAD, inp)
    return Decision(outcome=outcome, rule_id=rule_id, earned_bump=would_be == "bump")


__all__ = [
    "BUMPABLE_READINESS",
    "DELOAD_WEEK",
    "RULES",
    "AutoregInput",
    "Completion",
    "Decision",
    "Felt",
    "Outcome",
    "Readiness",
    "decide",
]
