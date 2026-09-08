"""The seven badges, derived on every read and stored nowhere.

Nothing here is persisted (PRP-10 risk 3): a badge is a question asked of ``session``,
``session_row``, ``planned_session`` and ``challenge`` at render time, so changing a rule is an
edit to this file rather than a migration plus a backfill.

Two definitions are deliberately **not** restated here:

- the streak comes from ``historique.scorecard`` (``streak_milestones``, one walk for all of it),
- "complete" comes from ``historique.queries.completion_threshold``, the same band threshold the
  History list judges a session by.

A badge that re-walked the plan would drift from the number on the card next to it (risk 4).

Prelude-ness is not a column. It lives in ``planned_session.rows_json`` under ``is_prelude``
(``programme.materialise``), paired to a stored row by position, which is what
``historique.detail.spec_index`` already does — so this module reads that index rather than
inventing a second parser. **A session carrying no prelude row at all does not qualify for
``prelude-4-weeks``** (D-231): "every prelude row is ticked" is vacuously true of a session with
none, and four weeks of prelude-less imported workouts would otherwise earn the warm-up badge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text
from sqlmodel import Session

from cadence.historique.clock import local_date, week_start
from cadence.historique.detail import spec_index
from cadence.historique.queries import completion_threshold, has_table
from cadence.historique.scorecard import streak_milestones
from cadence.profils.tables import Profile

YOUTH = "youth"

#: ``prelude-4-weeks`` wants this many ISO weeks with no gap between them.
PRELUDE_WEEKS = 4
#: The two count thresholds, and the two streak lengths.
SESSION_COUNTS = (10, 25)
STREAK_LENGTHS = (3, 7)


@dataclass(frozen=True, slots=True)
class Badge:
    """One badge as the strip renders it. ``earned_on`` is the qualifying day, never today."""

    id: str
    name: str
    earned: bool
    earned_on: date | None
    youth_safe: bool
    #: What fits inside a 56 px tile. The name is the caption, this is the face.
    short: str

    @property
    def caption(self) -> str:
        """The line that appears under the strip when a tile is tapped."""
        if not self.earned:
            return f"{self.name} · not yet"
        if self.earned_on is None:
            return f"{self.name} · earned"
        return f"{self.name} · earned {self.earned_on.strftime('%-d %b')}"


@dataclass(frozen=True, slots=True)
class _Rule:
    id: str
    adult: str
    youth: str
    short: str
    #: Whether a youth profile may be shown this badge at all. Declared per rule rather than
    #: assumed, so adding an unsafe one is a decision somebody writes down (D-254).
    youth_safe: bool


# All seven are youth-safe: every one counts sessions, weeks or ticked rows, and not one of them
# counts a kilogram, a percentage or a body part. ``challenge-met`` is safe because a
# body-composition challenge only ever exists on the adult profile (PRP-07, `goal_is_allowed`),
# so the badge can never name one for the son. The flag is what ``badges_for`` filters on; it is
# not decoration, and a rule added with ``youth_safe=False`` is dropped from every youth surface
# rather than merely renamed for it.
_RULES: tuple[_Rule, ...] = (
    _Rule("first-session", "First session", "You started!", "1st", youth_safe=True),
    _Rule("streak-3", "Three in a row", "Three in a row!", "3", youth_safe=True),
    _Rule("streak-7", "Seven in a row", "Seven in a row!", "7", youth_safe=True),
    _Rule("sessions-10", "Ten sessions", "Ten workouts!", "10", youth_safe=True),
    _Rule("sessions-25", "Twenty-five sessions", "Twenty-five workouts!", "25", youth_safe=True),
    _Rule("prelude-4-weeks", "Prelude, four weeks", "Warm-up champion", "warm", youth_safe=True),
    _Rule("challenge-met", "Challenge met", "You did it!", "goal", youth_safe=True),
)

_SESSIONS_SQL = """
SELECT s.id           AS session_id,
       s.finished_at  AS finished_at,
       COALESCE(p.rows_json, '') AS rows_json,
       r.position     AS position,
       r.done         AS done
FROM session s
LEFT JOIN planned_session p ON p.id = s.planned_session_id
LEFT JOIN session_row r ON r.session_id = s.id
WHERE s.profile_id = :profile_id AND s.finished_at IS NOT NULL
ORDER BY s.finished_at, r.position
"""

_MET_CHALLENGE_SQL = """
SELECT COUNT(*) AS met FROM challenge WHERE profile_id = :profile_id AND status = 'met'
"""


@dataclass(frozen=True, slots=True)
class _Finished:
    """One finished session, reduced to the three facts a badge asks about."""

    day: date | None
    complete: bool
    prelude_ok: bool


def _prelude_ok(specs: dict[int, dict[str, object]], ticks: dict[int, bool]) -> bool:
    """Every prelude row ticked — and there has to be at least one (D-231).

    Fails closed on purpose: a prelude position the specs declare but no ``session_row`` carries
    reads as unticked, because ``ticks.get`` defaults to ``False``. D-214 appends challenge rows
    to a stored ``rows_json``, so the two can disagree, and the safe reading of "we cannot tell
    whether the warm-up happened" is that the badge is not earned.
    """
    positions = [position for position, spec in specs.items() if spec.get("is_prelude")]
    if not positions:
        return False
    return all(ticks.get(position, False) for position in positions)


def _finished_sessions(db: Session, profile_id: str) -> list[_Finished]:
    """Every finished session for the profile, oldest first, in one statement.

    A household's history is small enough to reduce in Python, and the alternative — one query
    per session to reach its prelude rows — is the N+1 the History screen was built to avoid.
    """
    threshold = completion_threshold(db, profile_id)
    grouped: dict[str, dict[str, object]] = {}
    for row in db.execute(text(_SESSIONS_SQL), {"profile_id": profile_id}).all():
        mapping = row._mapping
        session_id = str(mapping["session_id"])
        bucket = grouped.setdefault(
            session_id,
            {"finished_at": mapping["finished_at"], "rows_json": str(mapping["rows_json"] or ""), "ticks": {}},
        )
        # A session with no rows at all joins to one null row, which has no position.
        if mapping["position"] is not None:
            ticks = bucket["ticks"]
            assert isinstance(ticks, dict)
            ticks[int(mapping["position"])] = bool(mapping["done"])

    finished: list[_Finished] = []
    for session_id, bucket in grouped.items():
        ticks = bucket["ticks"]
        assert isinstance(ticks, dict)
        specs = spec_index(str(bucket["rows_json"]), session_id)
        done = sum(1 for ticked in ticks.values() if ticked)
        finished.append(
            _Finished(
                day=local_date(bucket["finished_at"]),
                complete=done >= threshold,
                prelude_ok=_prelude_ok(specs, ticks),
            )
        )
    finished.sort(key=lambda item: (item.day is None, item.day or date.min))
    return finished


def _nth_complete_day(finished: list[_Finished], count: int) -> date | None:
    """The day the ``count``-th complete session landed, or ``None`` if there are fewer."""
    seen = 0
    for item in finished:
        if not item.complete:
            continue
        seen += 1
        if seen == count:
            return item.day
    return None


def prelude_streak_day(finished: list[_Finished]) -> date | None:
    """The last day of the first run of four consecutive ISO weeks that qualifies.

    Each of the four weeks needs at least one complete session, and **every** complete session in
    the window needs all of its prelude rows ticked and at least one of them to tick. A missing
    week breaks the run however many qualifying weeks sit either side of it.
    """
    weeks: dict[date, list[_Finished]] = {}
    for item in finished:
        if item.day is None or not item.complete:
            continue
        weeks.setdefault(week_start(item.day), []).append(item)
    if not weeks:
        return None

    for start in sorted(weeks):
        window = [start + timedelta(days=7 * offset) for offset in range(PRELUDE_WEEKS)]
        if not all(monday in weeks for monday in window):
            continue
        if not all(item.prelude_ok for monday in window for item in weeks[monday]):
            continue
        return max(item.day for item in weeks[window[-1]] if item.day is not None)
    return None


def _met_challenge(db: Session, profile_id: str) -> bool:
    """Whether any challenge is ``met``. Tolerates a database predating PRP-07's table."""
    if not has_table(db, "challenge"):
        return False
    row = db.execute(text(_MET_CHALLENGE_SQL), {"profile_id": profile_id}).first()
    return row is not None and int(row._mapping["met"] or 0) > 0


def badges_for(session: Session, profile_id: str) -> list[Badge]:
    """The seven badges for one profile, computed from scratch and written nowhere.

    Youth profiles get the playful names; everything else is identical, because no rule here
    references a body at all. **A rule that is not ``youth_safe`` is dropped here**, for a youth
    profile, before anything is built — so every caller inherits the filter rather than each one
    remembering it (D-256). That is why it is here and not in ``historique.page.build_column``,
    where the first cut put it: the History strip is only one of three youth surfaces that ask
    for badges, and the other two — the son's Done screen through ``badges_earned_on``, and the
    caption route through ``badge_by_id`` — went straight past a filter that lived on the column.

    ``challenge-met`` is earned without a date: the ``challenge`` table records no met-on day,
    and a guessed one is worse (D-232).

    **A badge is not monotonic, and cannot be** (D-248). "Complete" is judged against the band
    threshold the profile carries *now* (``completion_threshold``), and that threshold rises as
    the son moves up a band — 3 ticked rows at ``u10`` and ``age_10_13``, 4 at ``age_14_17``. So
    a fourteenth birthday can turn a past three-row session from complete to partial, which can
    un-earn ``first-session`` or ``sessions-10`` and move an ``earned_on`` forward. Flooring the
    result so an earned badge stays earned would need state, and PRP-10 risk 3 plus acceptance
    test 8 (``test_badges_are_derived_not_stored``) forbid exactly that — a stored badge is a
    migration and a backfill the next time a rule moves. The alternative was judged worse than
    the symptom: the son crosses one band boundary in this program's lifetime, the direction from
    youth to adult only *adds* badges (the adult threshold is 1), and the row data is untouched
    either way. Change: give ``Badge`` a stored floor, and own the migration.
    """
    profile = session.get(Profile, profile_id)
    youth = profile is not None and profile.kind == YOUTH
    finished = _finished_sessions(session, profile_id)
    streak, reached = streak_milestones(session, profile_id, STREAK_LENGTHS)
    complete_days = [item.day for item in finished if item.complete]

    earned_on: dict[str, date | None] = {
        "first-session": complete_days[0] if complete_days else None,
        "prelude-4-weeks": prelude_streak_day(finished),
    }
    won: dict[str, bool] = {
        "first-session": bool(complete_days),
        "prelude-4-weeks": earned_on["prelude-4-weeks"] is not None,
        "challenge-met": _met_challenge(session, profile_id),
    }
    for length in STREAK_LENGTHS:
        key = f"streak-{length}"
        won[key] = streak.best >= length
        earned_on[key] = reached[length]
    for count in SESSION_COUNTS:
        key = f"sessions-{count}"
        won[key] = len(complete_days) >= count
        earned_on[key] = _nth_complete_day(finished, count) if won[key] else None

    return [
        Badge(
            id=rule.id,
            name=rule.youth if youth else rule.adult,
            # ``get`` rather than ``[]``: a rule with no computation above is unearned, not a
            # crash. A badge nobody wrote a condition for has not been earned by anybody.
            earned=won.get(rule.id, False),
            earned_on=earned_on.get(rule.id) if won.get(rule.id, False) else None,
            youth_safe=rule.youth_safe,
            short=rule.short,
        )
        # The youth gate. Not a rename for the son, a removal: an unsafe badge never becomes a
        # ``Badge`` at all, so there is nothing downstream for a template to render by mistake.
        for rule in _RULES
        if rule.youth_safe or not youth
    ]


def badge_by_id(badges: list[Badge], badge_id: str) -> Badge | None:
    """The badge the caption route was asked for, or ``None`` for an id nobody ships."""
    return next((badge for badge in badges if badge.id == badge_id), None)


def badges_earned_on(session: Session, profile_id: str, day: date | None) -> list[Badge]:
    """The badges whose qualifying day is ``day`` — what a Done screen calls "new".

    Derived like everything else here: a badge earned on the day a session finished was earned by
    that session, so "new" needs no record of what the profile held yesterday. A badge with no
    date (``challenge-met``, D-232) is never new, because there is nothing to compare.
    """
    if day is None:
        return []
    return [badge for badge in badges_for(session, profile_id) if badge.earned and badge.earned_on == day]
