"""``load_library`` - YAML on disk to a validated, linked ``LibraryBundle``.

Fails loudly and completely. Any schema error, dangling link or failed validation aborts the whole
load with every problem listed as ``file:doc_id: message``; nothing is ever partially seeded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from cadence.bibliotheque.assessments import AssessmentLibrary
from cadence.bibliotheque.bundle import LibraryBundle, build_link_index
from cadence.bibliotheque.template import ProfileKind, WorkoutTemplate
from cadence.profils.settings import DEFAULT_SETTINGS, ProgramSettings
from cadence.profils.tables import Profile
from cadence.schema.enums import AgeBand
from cadence.schema.exercise import Exercise
from cadence.schema.progression import Progression
from cadence.schema.youth import YouthRules, YouthRuleSet
from cadence.validateur import validate_workout

LIBRARY_DIR = Path("library")
YOUTH_BANDS: tuple[AgeBand, ...] = (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17)
# Ages that land squarely inside each band, used to build the profiles the seed is validated for.
_BAND_AGE: dict[AgeBand, int] = {AgeBand.U10: 8, AgeBand.AGE_10_13: 11, AgeBand.AGE_14_17: 15}


class LibraryError(RuntimeError):
    """Every problem found in one load, so a broken library is fixed in one pass."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("library could not be loaded:\n  " + "\n  ".join(self.problems))


@dataclass(slots=True)
class _Collector:
    problems: list[str]

    def add(self, where: str, message: str) -> None:
        self.problems.append(f"{where}: {message}")

    def pydantic(self, where: str, exc: PydanticValidationError) -> None:
        for error in exc.errors():
            location = ".".join(str(part) for part in error.get("loc", ())) or "$"
            self.add(where, f"{location}: {error.get('msg', 'invalid value')}")


def _read_yaml(path: Path, problems: _Collector) -> dict[str, Any] | None:
    try:
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        problems.add(path.name, f"could not be read: {exc}")
        return None
    if not isinstance(parsed, dict):
        problems.add(path.name, "top level must be a mapping")
        return None
    return parsed


def _load_exercises(root: Path, problems: _Collector) -> dict[str, Exercise]:
    found: dict[str, Exercise] = {}
    for path in sorted((root / "exercises").glob("*.yaml")):
        doc = _read_yaml(path, problems)
        if doc is None:
            continue
        if doc.get("kind") != "exercises":
            problems.add(path.name, f"kind must be 'exercises', got {doc.get('kind')!r}")
            continue
        for entry in doc.get("exercises") or []:
            identifier = entry.get("id", "?") if isinstance(entry, dict) else "?"
            try:
                exercise = Exercise.model_validate(entry)
            except PydanticValidationError as exc:
                problems.pydantic(f"{path.name}:{identifier}", exc)
                continue
            if exercise.id in found:
                problems.add(f"{path.name}:{exercise.id}", "duplicate exercise id")
                continue
            found[exercise.id] = exercise
    return found


def _load_templates(root: Path, problems: _Collector) -> dict[str, WorkoutTemplate]:
    found: dict[str, WorkoutTemplate] = {}
    for path in sorted((root / "workouts").glob("*.yaml")):
        doc = _read_yaml(path, problems)
        if doc is None:
            continue
        try:
            template = WorkoutTemplate.model_validate(doc)
        except PydanticValidationError as exc:
            problems.pydantic(f"{path.name}:{doc.get('id', '?')}", exc)
            continue
        if template.id != path.stem:
            problems.add(f"{path.name}:{template.id}", "id must match the filename stem")
            continue
        if template.id in found:
            problems.add(f"{path.name}:{template.id}", "duplicate workout id")
            continue
        found[template.id] = template
    return found


def _load_progressions(root: Path, problems: _Collector) -> dict[str, Progression]:
    found: dict[str, Progression] = {}
    for path in sorted((root / "progressions").glob("*.yaml")):
        doc = _read_yaml(path, problems)
        if doc is None:
            continue
        if doc.get("kind") != "progressions":
            problems.add(path.name, f"kind must be 'progressions', got {doc.get('kind')!r}")
            continue
        for entry in doc.get("progressions") or []:
            identifier = entry.get("id", "?") if isinstance(entry, dict) else "?"
            try:
                progression = Progression.model_validate(entry)
            except PydanticValidationError as exc:
                problems.pydantic(f"{path.name}:{identifier}", exc)
                continue
            if progression.id in found:
                problems.add(f"{path.name}:{progression.id}", "duplicate progression id")
                continue
            found[progression.id] = progression
    return found


def _load_youth_rules(root: Path, problems: _Collector) -> dict[AgeBand, YouthRuleSet]:
    path = root / "youth_rules.yaml"
    doc = _read_yaml(path, problems)
    if doc is None:
        return {}
    try:
        parsed = YouthRules.model_validate(doc)
    except PydanticValidationError as exc:
        problems.pydantic(path.name, exc)
        return {}
    missing = [band.value for band in AgeBand if band not in parsed]
    if missing:
        problems.add(path.name, f"missing band(s): {', '.join(missing)}")
    return dict(parsed.root)


def _load_assessments(root: Path, problems: _Collector) -> AssessmentLibrary | None:
    path = root / "assessments.yaml"
    doc = _read_yaml(path, problems)
    if doc is None:
        return None
    try:
        return AssessmentLibrary.model_validate(doc)
    except PydanticValidationError as exc:
        problems.pydantic(path.name, exc)
        return None


def _check_links(
    exercises: dict[str, Exercise],
    templates: dict[str, WorkoutTemplate],
    progressions: dict[str, Progression],
    assessments: AssessmentLibrary | None,
    problems: _Collector,
) -> None:
    """Every id one document names must resolve to a document in this library."""
    for exercise in exercises.values():
        where = f"exercises:{exercise.id}"
        for field in ("regression_of", "progression_of"):
            target = getattr(exercise, field)
            if target is not None and target not in exercises:
                problems.add(where, f"{field} points at unknown exercise {target!r}")
        if exercise.default_progression and exercise.default_progression not in progressions:
            problems.add(where, f"default_progression {exercise.default_progression!r} is not in the library")

    for template in templates.values():
        where = f"workouts:{template.id}"
        if template.prelude is not None and template.prelude not in templates:
            problems.add(where, f"prelude {template.prelude!r} is not a workout in the library")
        for row in template.all_rows():
            named = [row.exercise, *(c.u10_sub for c in (row.adult, row.youth) if c and c.u10_sub)]
            if row.u10_sub:
                named.append(row.u10_sub)
            if row.anchor_alt is not None:
                named.append(row.anchor_alt.exercise)
            for rule in (row.load_rule, *(c.load_rule for c in (row.adult, row.youth) if c)):
                if rule is not None and rule.fallback_exercise:
                    named.append(rule.fallback_exercise)
            for identifier in named:
                if identifier not in exercises:
                    problems.add(where, f"row names unknown exercise {identifier!r}")

    if assessments is not None:
        for gap_id, gap in assessments.gap_rows.items():
            if gap.exercise is not None and gap.exercise not in exercises:
                problems.add(f"assessments:{gap_id}", f"gap row names unknown exercise {gap.exercise!r}")


def _validation_profile(kind: ProfileKind, band: AgeBand) -> Profile:
    """The profile a seed template is validated against: no bodyweight, no overhead anchor.

    Both are the shipped defaults, and both are the strict direction - a `conditional` exercise
    denies without a bodyweight, and every `requires_anchor` row is filtered out.
    """
    return Profile(
        id=f"seed-check-{kind}-{band.value}",
        display_name="seed check",
        kind=kind,
        age_years=_BAND_AGE.get(band),
        bodyweight_kg=None,
        has_overhead_anchor=False,
        age_band=band.value,
    )


def _validate_templates(bundle: LibraryBundle, settings: ProgramSettings, problems: _Collector) -> None:
    """Compile every template for every profile kind and band it claims to serve, and gate on ok.

    Gating on ``result.ok`` and never on ``len(result.errors)``: two codes are warnings that a
    perfectly good seed workout may legitimately carry.
    """
    from cadence.programme.materialise import compile_workout  # circular at module scope

    for template in bundle.templates.values():
        for kind in template.profile_kinds:
            bands = YOUTH_BANDS if kind == "youth" else (AgeBand.ADULT,)
            for band in bands:
                profile = _validation_profile(kind, band)
                where = f"workouts:{template.id}[{kind}/{band.value}]"
                try:
                    compiled = compile_workout(template, 1, profile, settings, bundle)
                except (PydanticValidationError, KeyError, ValueError) as exc:
                    problems.add(where, f"could not be compiled: {exc}")
                    continue
                result = validate_workout(
                    compiled,
                    profile_kind=kind,
                    age_band=band,
                    equipment=list(settings.equipment),
                    bodyweight_kg=profile.bodyweight_kg,
                    has_overhead_anchor=profile.has_overhead_anchor,
                    exercises=bundle.exercises,
                    youth_rules=bundle.youth_rules,
                )
                if not result.ok:
                    for error in result.errors:
                        if error.severity == "error":
                            problems.add(where, f"{error.path} {error.code}: {error.message}")


def load_library(path: Path = LIBRARY_DIR, settings: ProgramSettings | None = None) -> LibraryBundle:
    """Parse, link and validate everything under ``path``. Raises ``LibraryError`` on any problem."""
    problems = _Collector(problems=[])
    root = Path(path)
    if not root.is_dir():
        raise LibraryError([f"{root}: library directory not found"])

    exercises = _load_exercises(root, problems)
    templates = _load_templates(root, problems)
    progressions = _load_progressions(root, problems)
    youth_rules = _load_youth_rules(root, problems)
    assessments = _load_assessments(root, problems)
    if not exercises:
        problems.add("exercises", "no exercises found")
    if not templates:
        problems.add("workouts", "no workouts found")
    _check_links(exercises, templates, progressions, assessments, problems)
    if problems.problems or assessments is None:
        raise LibraryError(problems.problems or ["assessments.yaml: could not be loaded"])

    easier, harder = build_link_index(exercises)
    bundle = LibraryBundle(
        exercises=MappingProxyType(dict(exercises)),
        templates=MappingProxyType(dict(templates)),
        youth_rules=MappingProxyType(dict(youth_rules)),
        progressions=MappingProxyType(dict(progressions)),
        assessments=assessments,
        easier_than=MappingProxyType(easier),
        harder_than=MappingProxyType(harder),
    )
    _validate_templates(bundle, settings or DEFAULT_SETTINGS, problems)
    if problems.problems:
        raise LibraryError(problems.problems)
    return bundle
