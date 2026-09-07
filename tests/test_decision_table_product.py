"""The section 5.4 table over its **whole** input space, not one case per rule.

``tests/test_autoregulation.py`` asserts eleven hand-picked inputs, one chosen so that each rule
fires. That proves every rule is reachable; it cannot prove that no *other* input reaches the wrong
one. This file walks the full cartesian product of section 5.3's five inputs plus ``completion`` -
1 024 combinations - and checks each against a reference table transcribed from the principles
markdown rather than from ``cadence/programme/decision.py``.

Then four structural invariants that hold whatever order the table is written in, so a future edit
that keeps the product green by editing both copies still has to get past them:

- a bump requires every one of R9's conditions **and** an absence of the four rules above it;
- ``readiness == "unknown"`` is never treated as ``"low"`` (risk 6);
- missing more sessions never improves the outcome;
- a ``partial`` session never regresses at R2 or R7 (D-211, section 3.7's ``good_enough_done``).

``decide`` is a pure function of these six values and knows nothing about age bands - the youth
divergence lives entirely in ``apply_outcome`` - so the product runs once and not once per band.
"""

from __future__ import annotations

from itertools import product

import pytest

from cadence.programme.decision import AutoregInput, decide

TICKED = (True, False)
FELT: tuple[str | None, ...] = ("easy", "right", "hard", None)
READINESS = ("low", "ok", "high", "unknown")
MISSED = (0, 1, 2, 3)
WEEKS = (1, 2, 3, 4)
COMPLETION = ("complete", "partial")

BUMPABLE = frozenset({"ok", "high", "unknown"})
#: How bad an outcome is, for the monotonicity check. Higher is worse for the person training.
SEVERITY = {"bump": 0, "hold": 1, "deload": 2, "regress": 3}


def reference(
    ticked: bool, felt: str | None, readiness: str, missed: int, week: int, completion: str
) -> tuple[str, str]:
    """Principles section 5.4, transcribed from the document's own table.

    Deliberately a second copy. Its value is that it was typed from ``docs/exercise-principles.md``
    lines 330-342 and the PRP's two footnotes, not from the module under test, so a reordering of
    ``RULES`` - risk 1, the failure this whole file exists for - shows up as a disagreement rather
    than as two files edited into agreement.
    """
    # Section 5.4's footnote plus D-211: R2 and R7 both mean "the session was *full* and rows were
    # left unticked". A session ended early via ``good_enough_done`` reaches neither.
    unticked_full = (not ticked) and completion == "complete"

    if week == 4:
        return "R1", "deload"
    if felt == "hard" and unticked_full:
        return "R2", "regress"
    if missed >= 2:
        return "R3", "regress"
    if felt == "hard" and readiness == "low":
        return "R4", "regress"
    if felt == "hard":
        return "R5", "hold"
    if readiness == "low":
        return "R6", "hold"
    if unticked_full:
        return "R7", "hold"
    if missed == 1:
        return "R8", "hold"
    if ticked and felt == "easy" and readiness in BUMPABLE:
        return "R9", "bump"
    if ticked and felt == "right":
        return "R10", "hold"
    return "R11", "hold"


CASES = tuple(product(TICKED, FELT, READINESS, MISSED, WEEKS, COMPLETION))


def _input(case: tuple) -> AutoregInput:
    ticked, felt, readiness, missed, week, completion = case
    return AutoregInput(
        all_rows_ticked=ticked,
        felt=felt,  # type: ignore[arg-type]
        readiness=readiness,  # type: ignore[arg-type]
        missed_sessions_7d=missed,
        week_of_block=week,
        completion=completion,  # type: ignore[arg-type]
    )


def test_the_product_is_not_trivially_small() -> None:
    """A parametrisation that silently collapsed would make every assertion below vacuous."""
    assert len(CASES) == 2 * 4 * 4 * 4 * 4 * 2 == 1024
    fired = {reference(*case)[0] for case in CASES}
    assert fired == {f"R{index}" for index in range(1, 12)}, "the product does not reach every rule"


@pytest.mark.parametrize("case", CASES, ids=lambda case: "-".join(str(part) for part in case))
def test_every_input_matches_the_principles_table(case: tuple) -> None:
    """The full section 5.4 product: outcome, firing rule, and the bump week 4 stored."""
    rule_id, outcome = reference(*case)
    decision = decide(_input(case))

    assert (decision.rule_id, decision.outcome) == (rule_id, outcome)

    # Section 5.4: "the bump it earned is applied to week 1 of the next block instead". The flag is
    # what the table *would* have said with R1 removed, which is only ever interesting under R1.
    if outcome == "bump":
        expected_bump = True
    elif outcome == "deload":
        _, without_r1 = reference(case[0], case[1], case[2], case[3], week=1, completion=case[5])
        expected_bump = without_r1 == "bump"
    else:
        expected_bump = False
    assert decision.earned_bump is expected_bump, f"pending_bump is wrong for {case}"


def test_a_bump_needs_every_one_of_r9s_conditions() -> None:
    """Structural, not table-shaped: nothing outside R9's exact conditions may ever bump.

    Risk 2's first half. A youth load can only creep if a bump happens, so the set of inputs that
    can produce one is worth pinning independently of how the table is spelled.
    """
    for case in CASES:
        ticked, felt, readiness, missed, week, _ = case
        if decide(_input(case)).outcome != "bump":
            continue
        assert ticked, f"a session with rows left unticked bumped: {case}"
        assert felt == "easy", f"a session that did not feel easy bumped: {case}"
        assert readiness in BUMPABLE, f"a low-readiness session bumped: {case}"
        assert missed == 0, f"a session with a missed slot behind it bumped: {case}"
        assert week != 4, f"P2: week 4 bumped instead of deloading: {case}"


def test_unknown_readiness_is_never_read_as_low() -> None:
    """Risk 6. An unconnected household must not be held forever by an absent cache.

    Every input is run twice, once at ``low`` and once at ``unknown``. The unknown reading may
    never be the worse of the two, and on the R6/R9 pair it must be strictly better.
    """
    strictly_better = 0
    for ticked, felt, missed, week, completion in product(TICKED, FELT, MISSED, WEEKS, COMPLETION):
        low = decide(_input((ticked, felt, "low", missed, week, completion))).outcome
        unknown = decide(_input((ticked, felt, "unknown", missed, week, completion))).outcome
        assert SEVERITY[unknown] <= SEVERITY[low], f"unknown readiness was harsher than low: {(ticked, felt, missed)}"
        strictly_better += SEVERITY[unknown] < SEVERITY[low]
    assert strictly_better > 0, "unknown and low never diverge, so this test proves nothing"


def test_missing_more_sessions_never_helps() -> None:
    """R3 and R8 are penalties. Raising the count may not turn a hold into a bump."""
    for ticked, felt, readiness, week, completion in product(TICKED, FELT, READINESS, WEEKS, COMPLETION):
        outcomes = [
            decide(_input((ticked, felt, readiness, missed, week, completion))).outcome for missed in (0, 1, 2, 3)
        ]
        severities = [SEVERITY[outcome] for outcome in outcomes]
        assert severities == sorted(severities), f"more missed sessions improved the plan: {outcomes}"


def test_a_partial_session_never_regresses_at_r2_or_r7() -> None:
    """D-211 and P6. Stopping early is an exit the app offers; it is not a thing to punish.

    R3 still regresses a partial session - two missed slots are two missed slots however the third
    ended - so the assertion names the rules rather than the outcome.
    """
    penalised = 0
    for ticked, felt, readiness, week in product(TICKED, FELT, READINESS, WEEKS):
        partial = decide(_input((ticked, felt, readiness, 0, week, "partial")))
        assert partial.rule_id not in {"R2", "R7"}, f"a partial session was judged by {partial.rule_id}"
        full = decide(_input((ticked, felt, readiness, 0, week, "complete")))
        penalised += full.rule_id in {"R2", "R7"}
    assert penalised > 0, "no input reaches R2 or R7 at all, so the exclusion proves nothing"
