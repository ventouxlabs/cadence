"""The one pipeline behind ``POST /api/import`` and ``POST /api/generate/accept``.

Pure with respect to the filesystem: it takes ``text: str`` and never a path, so there is no
branch from an upload to ``open()`` and nothing for a crafted filename to traverse (D-010).

The generate path re-enters here rather than trusting its own preview. A preview is a document a
model wrote and a browser held for a while; re-serialising it and running every gate again is the
only version of "the client is not trusted" that stays true when somebody later adds a cache.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import yaml
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from cadence.bibliotheque import untrusted
from cadence.bibliotheque.untrusted import Finding
from cadence.db import ExerciseRecord, WorkoutRecord
from cadence.schema.enums import AgeBand, Equipment, ExerciseTag, YouthAllow
from cadence.schema.exercise import Exercise
from cadence.schema.workout import Workout
from cadence.schema.youth import YouthRuleSet
from cadence.validateur import codes as vcodes
from cadence.validateur import validate_exercise, validate_workout

logger = logging.getLogger(__name__)


class DuplicateId(RuntimeError):
    """Two writes reached the same primary key. Reported as ``id_collision``, never as a 500."""


SOURCE_IMPORT = "import"
SOURCE_GENERATED = "generated"
SOURCE_SEED = "seed"

# The document keys this pipeline understands. Anything else is dropped after being counted, so a
# pasted export from another app loses its extras instead of failing on them.
WORKOUT_KEYS: frozenset[str] = frozenset(Workout.model_fields)
INLINE_KEY = "exercises"
ALLOWED_TOP_LEVEL: frozenset[str] = WORKOUT_KEYS | {INLINE_KEY}

# At least one of these has to survive the strip, or the document was never a workout.
RECOGNISABLE: frozenset[str] = frozenset({"id", "name", "rows"})

# The three youth bands. ``AgeBand.ADULT`` is not one: the allowlist is a youth-only gate, and a
# document has nothing to say about the adult rule set either way.
YOUTH_BANDS: tuple[AgeBand, ...] = (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17)

# Tags that *grant* something rather than restrict it, and which a document therefore may not
# award itself. ``assessment_only`` lifts the max-effort ban on assessment day (D-054); ``play``
# satisfies the youth session's fun-row requirement. Every other tag only ever adds a restriction,
# so a document is welcome to claim one.
GRANTING_TAGS: frozenset[str] = frozenset({ExerciseTag.ASSESSMENT_ONLY.value, ExerciseTag.PLAY.value})

# The validator's own fallback when nothing says how long a set takes (``youth_rules``).
DEFAULT_EST_SECONDS_PER_SET = 45


@dataclass(frozen=True, slots=True)
class ImportTarget:
    """Everything the validator needs about the profile a document is being checked for.

    Every field is required. The validator defaults ``bodyweight_kg`` and ``has_overhead_anchor``,
    and an incomplete call there compiles while silently weakening V2, V13 and V14 - a
    ``conditional`` kettlebell row would be judged on the absolute cap alone (PRP-08 risk 10).
    Making them positional facts of a frozen object is what stops that call being writable.
    """

    profile_id: str
    profile_kind: Literal["adult", "youth"]
    age_band: AgeBand
    equipment: tuple[Equipment, ...]
    bodyweight_kg: float | None
    has_overhead_anchor: bool


@dataclass(frozen=True, slots=True)
class Prepared:
    """The outcome of running the gates. Nothing here has been written anywhere."""

    ok: bool
    errors: tuple[Finding, ...] = ()
    warnings: tuple[str, ...] = ()
    ignored_keys: tuple[str, ...] = ()
    workout: Workout | None = None
    inline_exercises: tuple[Exercise, ...] = ()
    document: Mapping[str, Any] = field(default_factory=dict)
    # The profile kind the document was actually validated against, recorded here at the moment
    # the check happens rather than re-derived at write time, so ``store`` cannot disagree with
    # the gate that ran (D-194).
    checked_profile_kind: str = ""

    def as_errors(self) -> list[dict[str, Any]]:
        return [item.as_dict() for item in self.errors]


def _fail(*errors: Finding, ignored: tuple[str, ...] = ()) -> Prepared:
    return Prepared(ok=False, errors=tuple(errors), ignored_keys=ignored)


def _from_validator(findings: list[Any]) -> tuple[tuple[Finding, ...], tuple[str, ...]]:
    """Split validator findings into blocking errors and human-readable warnings.

    Split on ``severity`` and never on the code, so this stays correct whichever way PRP-00
    settles the disagreement between its own docstring and its severity table.
    """
    errors = tuple(
        Finding(item.code, item.message, item.path, item.severity) for item in findings if item.severity == "error"
    )
    warnings = tuple(item.message for item in findings if item.severity != "error")
    return errors, warnings


def strip_code_fence(text: str) -> str:
    """Drop one leading and one trailing ``` fence. Defensive, for model output only.

    Not applied on the import path: a person pasting a fenced block gets a parse error naming the
    problem, which is more useful than a silent repair, and a repair on that path is one more
    transformation between what was reviewed and what was stored.
    """
    lines = text.strip().splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
    return "\n".join(lines)


def prepare(
    text: str,
    *,
    target: ImportTarget,
    catalog: Mapping[str, Exercise],
    youth_rules: Mapping[AgeBand, YouthRuleSet] | None,
    taken_workout_ids: frozenset[str] = frozenset(),
    taken_exercise_ids: frozenset[str] = frozenset(),
) -> Prepared:
    """Run every gate in the PRP's order and stop at the first one that trips.

    The one exception is the validator, which reports all of its findings at once: a person
    fixing a workout wants the whole list, not one error per round trip.
    """
    if len(text.encode("utf-8", "surrogatepass")) > untrusted.MAX_BODY_BYTES:
        return _fail(Finding(untrusted.TOO_LARGE, f"a workout must be under {untrusted.MAX_BODY_BYTES // 1024} KB"))
    if scanned := untrusted.scan_document(text):
        return _fail(*scanned)

    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # The reason, never the traceback and never the document: ``problem`` is PyYAML's own
        # one-line description and carries no payload of ours.
        reason = getattr(exc, "problem", None) or "it is not valid YAML or JSON"
        return _fail(Finding(untrusted.PARSE_ERROR, f"this document could not be read: {reason}"))
    except RecursionError:
        return _fail(Finding(untrusted.PARSE_ERROR, "this document nests too deeply to read"))

    if not isinstance(parsed, dict):
        kind = type(parsed).__name__
        return _fail(Finding(untrusted.NOT_A_MAPPING, f"a workout is a mapping of fields, not a {kind}"))
    if depth := untrusted.check_depth(parsed):
        return _fail(*depth)
    if counted := _check_counts(parsed):
        return _fail(*counted)
    if long := untrusted.check_string_lengths(parsed):
        return _fail(*long)
    if scanned := untrusted.scan_parsed(parsed):
        return _fail(*scanned)

    document, ignored = _strip_unknown(parsed)
    if not RECOGNISABLE & set(document):
        # ``yaml.safe_load("::: not yaml")`` is ``{'::': 'not yaml'}`` and a model's "Sure! Here is
        # your workout:" is a mapping too. Both are mappings of nothing, and reporting them as a
        # stack of missing required fields tells the reader the wrong thing about what went wrong.
        return _fail(
            Finding(
                untrusted.PARSE_ERROR,
                "this does not look like a workout: it names none of id, name or rows",
            ),
            ignored=ignored,
        )
    inline_raw = document.pop(INLINE_KEY, []) or []
    if inline_raw and target.profile_kind == "youth":
        # D-183. Everything the youth gate reads about a movement - the per-band allowlist, the
        # tags, the load type, the set clock - is exercise metadata, and an inline definition is
        # metadata the document wrote. Sanitising it is enough for an adult, whose rules read
        # almost none of it; for a child it leaves the gate arguing with the thing it is gating.
        # A workout for the son names movements that already exist, or it is not imported.
        return _fail(
            Finding(
                untrusted.INLINE_EXERCISE_NOT_ALLOWED_FOR_YOUTH,
                "a workout for a youth profile can only use exercises that are already in the "
                "library; it may not bring its own",
                INLINE_KEY,
            ),
            ignored=ignored,
        )
    inline, inline_errors = _parse_inline(inline_raw, taken_exercise_ids, catalog)
    if inline_errors:
        return _fail(*inline_errors, ignored=ignored)

    workout_id = document.get("id")
    if isinstance(workout_id, str) and workout_id in taken_workout_ids:
        return _fail(
            Finding(
                untrusted.ID_COLLISION,
                f"a workout called {workout_id!r} already exists; rename this one",
                "id",
            ),
            ignored=ignored,
        )

    merged: dict[str, Exercise] = {**catalog, **{item.id: item for item in inline}}
    result = validate_workout(
        document,
        profile_kind=target.profile_kind,
        age_band=target.age_band,
        equipment=list(target.equipment),
        bodyweight_kg=target.bodyweight_kg,
        has_overhead_anchor=target.has_overhead_anchor,
        exercises=merged,
        youth_rules=youth_rules,
    )
    errors, warnings = _from_validator(result.errors)
    # ``result.ok``, never ``not result.errors``: the result is a Pydantic model and always
    # truthy, and a warning-only document is a good document (PRP-00).
    if not result.ok:
        return Prepared(ok=False, errors=errors, warnings=warnings, ignored_keys=ignored)

    return Prepared(
        ok=True,
        warnings=warnings,
        ignored_keys=ignored,
        workout=Workout.model_validate(document),
        inline_exercises=tuple(inline),
        document=document,
        checked_profile_kind=target.profile_kind,
    )


def _check_counts(parsed: dict[str, Any]) -> list[Finding]:
    """The row and inline-exercise caps, checked before anything walks the document."""
    findings: list[Finding] = []
    for key in ("rows", INLINE_KEY):
        value = parsed.get(key)
        if isinstance(value, list) and len(value) > untrusted.MAX_EXERCISES:
            findings.append(
                Finding(
                    untrusted.TOO_MANY_EXERCISES,
                    f"{key} has {len(value)} entries; the limit is {untrusted.MAX_EXERCISES}",
                    key,
                )
            )
    return findings


def _strip_unknown(parsed: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Keep the keys this pipeline knows; report the rest rather than failing on them."""
    kept = {key: value for key, value in parsed.items() if key in ALLOWED_TOP_LEVEL}
    ignored = tuple(sorted(str(key) for key in parsed if key not in ALLOWED_TOP_LEVEL))
    return kept, ignored


def _est_seconds_floor(catalog: Mapping[str, Exercise], pattern: object) -> int:
    """The library's median ``est_seconds_per_set``, for this pattern where one exists.

    The validator times a session from this field, so a document that declares six seconds a set
    fits twenty rows inside a twenty-minute youth cap. The floor is read from the seed library
    rather than written here, so it tracks the library if the numbers there ever change.
    """
    same = sorted(
        item.est_seconds_per_set
        for item in catalog.values()
        if item.est_seconds_per_set is not None and item.pattern.value == pattern
    )
    values = same or sorted(item.est_seconds_per_set for item in catalog.values() if item.est_seconds_per_set)
    return values[len(values) // 2] if values else DEFAULT_EST_SECONDS_PER_SET


def _sanitise_inline(item: dict[str, Any], catalog: Mapping[str, Exercise]) -> dict[str, Any]:
    """Strip the safety metadata a document is not allowed to assert about itself (D-183).

    ``validate_exercise`` checks that a definition is *well formed*. It cannot check that it is
    *true*: the per-band allowlist, the prelude flag, the assessment exemption and the set clock
    are all facts the validator later reads back as permission, so a document that supplies them
    is grading its own homework. Every one of them is replaced with the closed value here, before
    the model is built, because the model is frozen and a later ``model_copy`` would skip
    validation.
    """
    clean = dict(item)
    # Fails closed for every youth band, whatever the document said. This is the field that
    # matters most: an imported exercise outlives its document in the `exercise` table, and a
    # *later* youth workout naming it would be judged on this dict.
    clean["youth_ok_by_band"] = dict.fromkeys(YOUTH_BANDS, YouthAllow.NO.value)
    # A prelude row does not count against the youth exercise cap, so declaring twelve of them is
    # how a session escapes V6.
    clean["is_prelude"] = False
    raw_tags = clean.get("tags")
    if isinstance(raw_tags, list | tuple):
        clean["tags"] = [tag for tag in raw_tags if tag not in GRANTING_TAGS]
    floor = _est_seconds_floor(catalog, clean.get("pattern"))
    current = clean.get("est_seconds_per_set")
    if isinstance(current, bool) or not isinstance(current, int) or current < floor:
        clean["est_seconds_per_set"] = floor
    return clean


def _parse_inline(
    raw: Any,
    taken_exercise_ids: frozenset[str],
    catalog: Mapping[str, Exercise],
) -> tuple[list[Exercise], list[Finding]]:
    """Validate the document's own exercise definitions, with their safety claims removed.

    An inline definition may not shadow one that already exists: a document that could redefine
    ``push-up`` could give it ``load_type: bodyweight`` and a kettlebell's actual load, and every
    band check downstream would agree with it. Nor may two definitions in one document share an
    id — the second would silently win the merge and then collide on the primary key at write.
    """
    if not isinstance(raw, list):
        return [], [Finding(vcodes.SCHEMA, "exercises must be a list", INLINE_KEY)]
    exercises: list[Exercise] = []
    findings: list[Finding] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        path = f"{INLINE_KEY}[{index}]"
        if not isinstance(item, dict):
            findings.append(Finding(vcodes.SCHEMA, "an inline exercise is a mapping of fields", path))
            continue
        clean = _sanitise_inline(item, catalog)
        result = validate_exercise(clean)
        if not result.ok:
            findings.extend(
                Finding(err.code, err.message, f"{path}.{err.path}", err.severity)
                for err in result.errors
                if err.severity == "error"
            )
            continue
        exercise = Exercise.model_validate(clean)
        if exercise.id in taken_exercise_ids:
            findings.append(
                Finding(
                    untrusted.ID_COLLISION,
                    f"an exercise called {exercise.id!r} already exists; this document may not redefine it",
                    path,
                )
            )
            continue
        if exercise.id in seen:
            findings.append(
                Finding(
                    untrusted.ID_COLLISION,
                    f"this document defines {exercise.id!r} twice",
                    path,
                )
            )
            continue
        seen.add(exercise.id)
        exercises.append(exercise)
    return exercises, findings


# ------------------------------------------------------------------------------ database access


def exercise_catalog(session: Session) -> dict[str, Exercise]:
    """Every stored exercise, parsed. The catalog the validator resolves rows against.

    Read from the database rather than from ``library/`` so an exercise a previous import defined
    inline resolves for the next one, and so nothing on this path touches a file.
    """
    catalog: dict[str, Exercise] = {}
    for record in session.exec(select(ExerciseRecord)).all():
        try:
            catalog[record.id] = Exercise.model_validate(json.loads(record.doc_json))
        except (TypeError, ValueError):
            # A row that will not parse is not a row the gate may treat as permission. It is
            # dropped, so a workout naming it fails `unknown_exercise` rather than resolving
            # against a half-read definition.
            logger.warning("stored exercise %r will not parse and is ignored by the import gate", record.id)
    return catalog


def seed_exercise_ids(session: Session) -> frozenset[str]:
    """The ids ``make seed`` loaded from ``library/``, in one query.

    This is what the generation prompt is allowed to offer. The full catalog contains every
    exercise an import ever defined inline, and those ids are attacker-chosen text: naming them to
    a remote model is a channel out of the house that nothing in the prompt's placeholder list
    accounts for (Codex finding 6).
    """
    rows = session.exec(select(ExerciseRecord.id).where(ExerciseRecord.source == SOURCE_SEED)).all()
    return frozenset(str(row) for row in rows)


def taken_workout_ids(session: Session) -> frozenset[str]:
    return frozenset(record.id for record in session.exec(select(WorkoutRecord)).all())


def taken_exercise_ids(session: Session) -> frozenset[str]:
    return frozenset(record.id for record in session.exec(select(ExerciseRecord)).all())


def free_workout_id(wanted: str, taken: frozenset[str]) -> str:
    """``wanted`` if nothing holds it, else the first free ``-2``, ``-3`` … suffix.

    Only the generate path uses this. An import keeps the id it was given and is refused on a
    collision: renaming a document somebody wrote by hand hides the clash rather than reporting it.
    """
    if wanted not in taken:
        return wanted
    for suffix in range(2, 1000):
        candidate = f"{wanted}-{suffix}"
        if candidate not in taken:
            return candidate
    raise ValueError("no free workout id remains")


def store(session: Session, prepared: Prepared, *, source: str) -> str:
    """Write the workout and any inline exercises. One transaction, seed rows never touched."""
    if prepared.workout is None:  # pragma: no cover - guarded by every caller
        raise ValueError("nothing to store: this document did not pass the gates")
    stamp = datetime.now(UTC).isoformat()
    for exercise in prepared.inline_exercises:
        session.add(
            ExerciseRecord(
                id=exercise.id,
                doc_json=json.dumps(exercise.model_dump(mode="json"), sort_keys=True),
                source=source,
                created_at=stamp,
            )
        )
    workout = prepared.workout
    session.add(
        WorkoutRecord(
            id=workout.id,
            doc_json=json.dumps(workout.model_dump(mode="json"), sort_keys=True),
            source=source,
            # The kind it was actually *checked* against, not the kind it claimed. A ``both``
            # document imported for the adult is judged under adult rules alone, so storing
            # ``both`` would record a youth clearance no youth rule ever granted. Reading that
            # column later - PRP-10 plans to - would then hand the son a workout on the strength
            # of the document's own say-so (D-194).
            target_profile_kind=prepared.checked_profile_kind,
            created_at=stamp,
        )
    )
    try:
        session.commit()
    except IntegrityError as exc:
        # The id checks upstream read the table a moment earlier, so a second import of the same
        # document racing this one still reaches the primary key. That is a collision, not a
        # server fault, and it answers like every other collision (Codex finding 13).
        session.rollback()
        raise DuplicateId(f"{workout.id!r} was written by something else while this import ran") from exc
    logger.info("stored %s workout %r with %d rows", source, workout.id, len(workout.rows))
    return workout.id
