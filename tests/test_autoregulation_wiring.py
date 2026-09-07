"""The parts of autoregulation that only a database can show are wired up.

``tests/test_autoregulation.py`` exercises ``decide``, ``apply_outcome`` and ``consume_pending_bump``
as pure functions, and every one of them passes against a caller that never invokes it. This file
covers the seams between them:

- ``pending_bump`` read off the **finished** row while ``week`` comes off the **target** row, which
  is the two-hop nobody would notice was crossed (acceptance test 4 cannot see it);
- the stored week-4 prescription against section 6.2's own numbers, rather than section 5.5's
  arithmetic over a row built by hand (D-218(a) is precisely the gap between those two);
- a hold leaving the prescription alone, checked against a bump over the same rows;
- a Together Done nudging **both** people's plans, not just the one whose button was tapped;
- PRP-06's ``{"score": null}`` readiness - the son's permanent state - reading ``unknown``, so R9
  can still bump him and R6 does not hold him forever (D-219(b), risk 6).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlmodel import select

from cadence.bibliotheque.loader import load_library
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from cadence.programme.autoregulation import next_of_day_type, rewrite_rows
from cadence.programme.context import make_context
from cadence.programme.decision import AutoregInput, Decision, decide
from cadence.programme.materialise import materialise_rows
from cadence.programme.schemes import workout_id_for
from cadence.programme.signals import readiness_for
from cadence.programme.tables import PlannedSession
from cadence.schema.enums import DayType
from cadence.seance.catalog import load_settings
from cadence.seance.tables import SessionRecord
from cadence.seance.today import resolve_today
from cadence.vitalforge.tables import MetricsCache

LIBRARY = Path(__file__).resolve().parents[1] / "library"

#: The fields that *are* the prescription. A hold may rewrite ``last_outcome``; it may not touch
#: one of these, so a hold is compared on them rather than on the JSON blob.
PRESCRIPTION = ("exercise_id", "sets", "reps", "seconds", "meters", "load_kg", "rpe_target")


def _ctx(db: Any, profile_id: str = PROFILE_ME) -> tuple[Profile, Any, Any]:
    library = load_library(LIBRARY)
    profile = db.get(Profile, profile_id)
    return profile, make_context(profile, load_settings(db), library), library


def _prescription(rows: list[dict]) -> dict[int, tuple]:
    return {int(row["position"]): tuple(row.get(key) for key in PRESCRIPTION) for row in rows}


def _by_position(rows: list[dict]) -> dict[int, dict]:
    return {int(row["position"]): row for row in rows}


def _working(rows: list[dict]) -> list[dict]:
    """The rows autoregulation is allowed to touch.

    Section 2's prelude is fixed, so ``apply_outcome`` returns it untouched - which means it never
    grows ``pending_bump`` or ``last_outcome`` either. Asserting those keys over a prelude row is
    asserting that the prelude was progressed, which is the opposite of the rule.
    """
    return [row for row in rows if not row.get("is_prelude")]


# ------------------------------------------------- the pending bump, across the two-hop it lives on


def test_a_week_four_deload_stores_the_bump_on_the_row_it_writes(db_session: Any, seeded: Any) -> None:
    """R1 suppresses the bump; the flag has to land on the row the next block will read.

    Acceptance test 4 calls ``consume_pending_bump`` directly and so proves only that the function
    works. What it cannot show is that anything ever *sets* the flag on a stored row - and
    ``rewrite_rows`` takes ``earned_bump`` from the decision while taking ``week`` from the target,
    so the two halves are wired in different places.
    """
    profile, ctx, library = _ctx(db_session)
    template = library.template(workout_id_for(DayType.UPPER_A, "adult"))
    week_three = materialise_rows(template, 3, profile, ctx.settings, library)
    week_four = materialise_rows(template, 4, profile, ctx.settings, library)

    decision = decide(AutoregInput(all_rows_ticked=True, felt="easy", readiness="ok", week_of_block=4))
    assert (decision.outcome, decision.earned_bump) == ("deload", True), "the fixture must earn a bump to suppress"

    rows, moved = rewrite_rows(week_three, week_four, decision, ctx, week=4)

    touched = _working([row for _, row in moved])
    assert touched, "no working row was paired, so nothing below is being asserted about"
    assert all(row["pending_bump"] is True for row in touched), "the earned bump was dropped, not stored"
    assert all(row["last_outcome"] == "deload" for row in touched)
    assert all(row.get("pending_bump") is not True for row in week_three), "rewrite_rows mutated its source"


def test_the_stored_bump_is_applied_and_cleared_at_week_one(db_session: Any, seeded: Any) -> None:
    """Week 1 of the next block raises the prescription once and clears the flag."""
    profile, ctx, library = _ctx(db_session)
    template = library.template(workout_id_for(DayType.UPPER_A, "adult"))
    week_one = materialise_rows(template, 1, profile, ctx.settings, library)
    carried = [{**row, "pending_bump": True} for row in materialise_rows(template, 4, profile, ctx.settings, library)]

    # A plain hold at week 1: nothing but the stored bump may move the prescription.
    rows, moved = rewrite_rows(carried, week_one, Decision("hold", "R11"), ctx, week=1)
    after = {int(row["position"]): row for row in rows}

    assert moved
    assert all(row["pending_bump"] is False for row in _working(rows)), "the flag survived the block it spent"
    now = _prescription(rows)
    raised = [position for position, before in _prescription(week_one).items() if before != now[position]]
    assert raised, "the carried bump changed nothing, so week 4's suppression simply lost it"
    for position in raised:
        assert after[position]["last_outcome"] == "hold", "the week-1 outcome is still the table's, not the bump's"


def test_the_stored_bump_clears_even_when_week_one_regresses(db_session: Any, seeded: Any) -> None:
    """A comeback week that regresses must not leave the flag armed for the week after."""
    profile, ctx, library = _ctx(db_session)
    template = library.template(workout_id_for(DayType.UPPER_A, "adult"))
    week_one = materialise_rows(template, 1, profile, ctx.settings, library)
    carried = [{**row, "pending_bump": True} for row in materialise_rows(template, 4, profile, ctx.settings, library)]

    rows, moved = rewrite_rows(carried, week_one, Decision("regress", "R3"), ctx, week=1)

    assert moved
    assert all(row["pending_bump"] is False for row in _working(rows)), "a regressed week 1 kept the bump"


def test_a_bump_is_not_carried_when_the_target_is_not_week_one(db_session: Any, seeded: Any) -> None:
    """The flag is consumed by week 1 and by nothing else, or a block bumps twice over."""
    profile, ctx, library = _ctx(db_session)
    template = library.template(workout_id_for(DayType.UPPER_A, "adult"))
    week_two = materialise_rows(template, 2, profile, ctx.settings, library)
    carried = [{**row, "pending_bump": True} for row in materialise_rows(template, 1, profile, ctx.settings, library)]

    rows, _ = rewrite_rows(carried, week_two, Decision("hold", "R11"), ctx, week=2)

    assert _prescription(rows) == _prescription(week_two), "week 2 spent a bump that belongs to week 1"


# --------------------------------------------------- the stored week-4 shape, against section 6.2


def test_the_stored_week_four_prescription_matches_section_62(db_session: Any, seeded: Any) -> None:
    """Two sets, the week-1 reps, the same load, RPE 6, and the carry at 60% of week 3.

    Acceptance test 10 asserts section 5.5's arithmetic over a row it builds itself, which is a row
    the database never holds. This asserts the numbers that actually reach the plan: section 6.2's
    week-4 line (2 x 8, RPE 6, carries W3 x 0.60 == the W1 distance) after R1 has run over rows
    ``materialise_rows`` already shaped for week 4 (D-218(a)).
    """
    profile, ctx, library = _ctx(db_session)
    template = library.template(workout_id_for(DayType.LOWER_FULL_B, "adult"))
    week_one = {row["position"]: row for row in materialise_rows(template, 1, profile, ctx.settings, library)}
    week_three = materialise_rows(template, 3, profile, ctx.settings, library)
    week_four = materialise_rows(template, 4, profile, ctx.settings, library)

    rows, moved = rewrite_rows(week_three, week_four, Decision("deload", "R1", earned_bump=False), ctx, week=4)
    after, was_three, was_four = _by_position(rows), _by_position(week_three), _by_position(week_four)

    assert moved
    working = _working(rows)
    assert working, "a session of nothing but prelude would pass every assertion below vacuously"

    carries = 0
    for row in working:
        position = int(row["position"])
        assert row["sets"] <= 2, f"week 4 is a two-set week, not {row['sets']}"
        if row.get("rpe_target") is not None:
            assert row["rpe_target"] <= 6, "section 6.2 caps week 4 at RPE 6"
        if row.get("reps") is not None:
            assert row["reps"] == week_one[position]["reps"], "week 4 returns to the week-1 reps"
        if row.get("meters") is not None and was_three.get(position, {}).get("meters") is not None:
            # W3 3 x 50 m, W4 2 x 30 m: 50 x 0.60 is 30, so section 5.5 and section 6.2 agree here
            # and the number has to be reached exactly once, never twice (D-218(a)).
            carries += 1
            assert row["meters"] == week_one[position]["meters"]
            assert row["meters"] == round(float(was_three[position]["meters"]) * 0.60, 1)
    assert carries, "this template carries no distance row, so the compounding check is vacuous"

    assert {position: row.get("load_kg") for position, row in after.items()} == {
        position: row.get("load_kg") for position, row in was_four.items()
    }, "a deload changes the volume, never the weight"


# ------------------------------------------------------------- a hold leaves the plan where it is


def test_a_missed_session_holds_the_prescription(db_session: Any, seeded: Any) -> None:
    """R8: one missed slot holds. The next session keeps every number it was built with.

    Asserted on the prescription fields rather than on ``rows_json`` bytes, because a hold does
    legitimately rewrite ``last_outcome``. The rule id is pinned from ``decide`` as well, so a hold
    reached because ``rows_pass_validator`` rejected the rewrite cannot pass for the right one.
    """
    profile, ctx, library = _ctx(db_session)
    decision = decide(AutoregInput(all_rows_ticked=True, felt="easy", readiness="ok", missed_sessions_7d=1))
    assert (decision.outcome, decision.rule_id) == ("hold", "R8"), "the fixture is not exercising R8"

    template = library.template(workout_id_for(DayType.UPPER_A, "adult"))
    source = materialise_rows(template, 2, profile, ctx.settings, library)
    target = materialise_rows(template, 2, profile, ctx.settings, library)

    rows, _ = rewrite_rows(source, target, decision, ctx, week=2)

    assert _prescription(rows) == _prescription(target), "a held session changed the numbers"
    assert all(row["last_outcome"] == "hold" for row in _working(rows))
    assert _prescription(rows) != _prescription(
        rewrite_rows(source, target, Decision("bump", "R9", earned_bump=True), ctx, week=2)[0]
    ), "hold and bump produce the same rows, so the hold assertion above proves nothing"


def test_only_the_next_untouched_session_of_that_day_type_is_targeted(db_session: Any, seeded: Any) -> None:
    """Risk 4. A checklist somebody has started ticking is never rewritten underneath them."""
    finished = db_session.exec(
        select(PlannedSession)
        .where(PlannedSession.profile_id == PROFILE_ME, PlannedSession.day_type != "assessment")
        .order_by(PlannedSession.week, PlannedSession.day_index)  # type: ignore[arg-type]
    ).first()
    assert finished is not None

    target = next_of_day_type(db_session, PROFILE_ME, finished)
    assert target is not None and target.day_type == finished.day_type
    assert (target.week, target.day_index) > (finished.week, finished.day_index), "the queue ran backwards"

    started = SessionRecord(
        id="started-by-somebody",
        profile_id=PROFILE_ME,
        planned_session_id=target.id,
        started_at="2026-09-06T18:00:00+00:00",
    )
    db_session.add(started)
    db_session.commit()

    skipped = next_of_day_type(db_session, PROFILE_ME, finished)
    assert skipped is None or skipped.id != target.id, "a session with a tick on it was chosen anyway"


# --------------------------------------------------------- a shared Done nudges both people's plans


async def test_a_together_done_autoregulates_both_sessions(seeded_client: httpx.AsyncClient, db_session: Any) -> None:
    """D-013: one Done finalises both. PRP-07 has to follow it with two nudges, not one.

    ``finish`` loops ``after_done`` over the sessions it transitioned, so a hook wired to
    ``view.record`` alone would leave the son's plan frozen for ever while the parent's moved -
    silently, because the Done screen renders both cards either way.
    """
    # The seeded block opens on the assessment day, which runs no autoregulation (section 10.10).
    for profile_id in (PROFILE_ME, PROFILE_SON):
        view = resolve_today(db_session, profile_id).primary
        assert view.planned.day_type == "assessment"
        response = await seeded_client.post(f"/api/sessions/{view.record.id}/done", json={"felt": "right"})
        assert response.status_code == 200

    data = (await seeded_client.get("/api/today?profile=together")).json()["data"]
    assert len(data["sessions"]) == 2, "the fixture did not plan a shared day"
    assert all(session["day_type"] != "assessment" for session in data["sessions"])

    for session in data["sessions"]:
        for row in session["rows"]:
            reply = await seeded_client.post(
                f"/api/sessions/{session['session_id']}/rows/{row['position']}", json={"done": True}
            )
            assert reply.status_code == 200

    before = {planned.id: planned.rows_json for planned in db_session.exec(select(PlannedSession)).all()}

    tapped = data["sessions"][0]["session_id"]
    done = await seeded_client.post(f"/api/sessions/{tapped}/done", json={"felt": "easy"})
    assert done.status_code == 200

    records = {
        record.profile_id: record
        for record in db_session.exec(
            select(SessionRecord).where(
                SessionRecord.id.in_(
                    [  # type: ignore[union-attr]
                        session["session_id"] for session in data["sessions"]
                    ]
                )
            )
        ).all()
    }
    assert set(records) == {PROFILE_ME, PROFILE_SON}, "the shared Done did not finalise both"
    for profile_id, record in records.items():
        assert record.finished_at is not None
        assert record.notes, f"{profile_id} got no next-time line"

    # A truthy ``notes`` is not enough: ``autoregulate_next`` returns HOLD_NOTE when it finds no
    # target at all, and ``_after_done`` stores that line either way. Only a changed ``rows_json``
    # says a plan was really rewritten - and it has to be one per person, not two for the parent.
    db_session.expire_all()
    rewritten: dict[str, int] = {PROFILE_ME: 0, PROFILE_SON: 0}
    for planned in db_session.exec(select(PlannedSession)).all():
        if before.get(planned.id) != planned.rows_json:
            rewritten[planned.profile_id] = rewritten.get(planned.profile_id, 0) + 1
    assert rewritten == {PROFILE_ME: 1, PROFILE_SON: 1}, (
        f"the shared Done nudged {rewritten}, not one session of the same day type for each person"
    )


# ------------------------------------------------- PRP-06's null readiness, which is the son's for ever


def _cache(db: Any, profile_id: str, readiness: dict) -> None:
    db.merge(
        MetricsCache(
            profile_id=profile_id,
            fetched_at="2026-09-06T06:00:00+00:00",
            payload_json=json.dumps({"readiness": readiness}),
        )
    )
    db.commit()


def test_a_null_readiness_score_reads_unknown_and_still_bumps(db_session: Any, seeded: Any) -> None:
    """D-219(b). VitalForge answers the son ``{"score": null, "status": "insufficient_data"}``.

    Permanently: he wears no watch. Reading that as ``low`` would hold every one of his sessions at
    R6 for the life of the install and no screen would ever say why (risk 6). Reading it as a
    *number* would be worse still, because zero bands to ``low``.
    """
    payload = {"score": None, "status": "insufficient_data"}
    for profile_id in (PROFILE_ME, PROFILE_SON):
        _cache(db_session, profile_id, payload)
        assert readiness_for(db_session, profile_id) == "unknown", f"{profile_id} was banded off a null score"

    decision = decide(AutoregInput(all_rows_ticked=True, felt="easy", readiness="unknown", week_of_block=1))
    assert (decision.outcome, decision.rule_id) == ("bump", "R9"), "R9 must accept an unknown readiness"


def test_a_real_low_score_still_holds(db_session: Any, seeded: Any) -> None:
    """The null path above is only safe because a genuine low reading still reaches R6."""
    from cadence.vitalforge.metrics import STEADY_AT

    _cache(db_session, PROFILE_ME, {"score": STEADY_AT - 1, "status": "low"})
    assert readiness_for(db_session, PROFILE_ME) == "low"

    decision = decide(AutoregInput(all_rows_ticked=True, felt="easy", readiness="low", week_of_block=1))
    assert (decision.outcome, decision.rule_id) == ("hold", "R6")


def test_a_status_only_payload_is_not_mistaken_for_a_score(db_session: Any, seeded: Any) -> None:
    """A cache written before PRP-06 added the score carries a status and nothing else."""
    _cache(db_session, PROFILE_ME, {"status": "high"})
    assert readiness_for(db_session, PROFILE_ME) == "high"

    _cache(db_session, PROFILE_ME, {"status": "whatever-vitalforge-called-it"})
    assert readiness_for(db_session, PROFILE_ME) == "unknown", "an unrecognised status must not band to low"


# ---------------------------------------------- the 28-day retest reminder, and what gates it


def test_the_retest_falls_due_exactly_twenty_eight_days_after_the_newest_battery(db_session: Any, seeded: Any) -> None:
    """``is_due`` turns true **on** the due day, not the day after (PRP-07: ">= 28 days old").

    The boundary is worth pinning on its own: an off-by-one here is a reminder that arrives a day
    late every cycle and that nothing else in the suite would notice.
    """
    from cadence.bilan import assessments as bilan
    from cadence.bilan.assessments import RETEST_DAYS
    from cadence.bilan.tables import Assessment

    me = db_session.get(Profile, PROFILE_ME)
    baseline = date(2026, 9, 6)
    assert bilan.is_due(db_session, me.id, baseline), "no baseline yet, so the card is always due"

    db_session.add(
        Assessment(
            id="me-plank-baseline",
            profile_id=me.id,
            test_id="plank_s",
            value=90.0,
            unit="s",
            recorded_on=baseline.isoformat(),
        )
    )
    db_session.commit()

    due = baseline + timedelta(days=RETEST_DAYS)
    assert bilan.next_due_on(db_session, me.id) == due
    assert bilan.is_due(db_session, me.id, due - timedelta(days=1)) is False
    assert bilan.is_due(db_session, me.id, due) is True, "the retest is due on day 28, not day 29"
    assert bilan.is_due(db_session, me.id, due + timedelta(days=1)) is True


def test_the_card_returns_only_when_the_plan_has_an_assessment_day_queued(db_session: Any, seeded: Any) -> None:
    """``card_due`` gates the reminder on the queue as well as on the calendar.

    Documented in ``card_due``'s own docstring and asserted here so the coupling is explicit: once
    a baseline exists, being 28 days overdue is **not** enough - the head of the queue has to be an
    assessment day. See the report for why that makes the second reminder unreachable inside a
    single seeded four-week block.
    """
    from cadence.bilan.assessments import RETEST_DAYS
    from cadence.bilan.service import card_due
    from cadence.bilan.tables import Assessment

    me = db_session.get(Profile, PROFILE_ME)
    head = db_session.exec(
        select(PlannedSession)
        .where(PlannedSession.profile_id == PROFILE_ME, PlannedSession.status == "planned")
        .order_by(PlannedSession.week, PlannedSession.day_index)  # type: ignore[arg-type]
    ).first()
    assert head is not None and head.day_type == "assessment", "the seeded block opens on the assessment day"

    baseline = date(2026, 9, 6)
    overdue = baseline + timedelta(days=RETEST_DAYS + 1)
    db_session.add(
        Assessment(
            id="me-plank-baseline",
            profile_id=me.id,
            test_id="plank_s",
            value=90.0,
            unit="s",
            recorded_on=baseline.isoformat(),
        )
    )
    db_session.commit()

    assert card_due(db_session, me, overdue) is True, "overdue, with the assessment day at the head"

    head.status = "done"
    db_session.add(head)
    db_session.commit()

    assert card_due(db_session, me, overdue) is False, (
        "the calendar alone brings the card back, which would show it over a day that is not one"
    )
