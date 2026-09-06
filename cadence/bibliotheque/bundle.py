"""``LibraryBundle`` - everything under ``library/``, parsed, linked and ready to compile."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping  # noqa: UP035 - MappingProxyType needs the ABC, not dict

from cadence.bibliotheque.assessments import AssessmentLibrary
from cadence.bibliotheque.template import WorkoutTemplate
from cadence.schema.enums import AgeBand, ExerciseTag
from cadence.schema.exercise import Exercise
from cadence.schema.progression import Progression
from cadence.schema.youth import YouthRuleSet


@dataclass(frozen=True, slots=True)
class LibraryBundle:
    """The whole seed library. Immutable: every mapping is a read-only view.

    ``easier_than`` / ``harder_than`` are the transpose of the declared links, held here rather
    than written back into ``Exercise`` (D-052): the model is frozen and carries one slot per
    direction, while section 9's edges are many-to-one in four places. Both declared directions
    mean the same thing - ``x.regression_of = y`` and ``y.progression_of = x`` both say x is
    easier than y - so they collapse into one graph.
    """

    exercises: Mapping[str, Exercise]
    templates: Mapping[str, WorkoutTemplate]
    youth_rules: Mapping[AgeBand, YouthRuleSet]
    progressions: Mapping[str, Progression]
    assessments: AssessmentLibrary
    easier_than: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    harder_than: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def exercise(self, exercise_id: str) -> Exercise:
        found = self.exercises.get(exercise_id)
        if found is None:
            raise KeyError(f"{exercise_id!r} is not an exercise in the library")
        return found

    def template(self, template_id: str) -> WorkoutTemplate:
        found = self.templates.get(template_id)
        if found is None:
            raise KeyError(f"{template_id!r} is not a workout template in the library")
        return found

    def progression(self, progression_id: str | None) -> Progression | None:
        return self.progressions.get(progression_id) if progression_id else None

    def play_pool(self) -> tuple[Exercise, ...]:
        """The fun pool of principles section 6.5, in a fixed order (id ascending)."""
        return tuple(
            self.exercises[key] for key in sorted(self.exercises) if ExerciseTag.PLAY in self.exercises[key].tags
        )


LinkIndex = tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]


def build_link_index(exercises: Mapping[str, Exercise]) -> LinkIndex:
    """Transpose the declared links into ``easier_than`` and ``harder_than``.

    Both maps are keyed by exercise id and sorted, so two loads of the same library produce
    byte-identical output.
    """
    easier: dict[str, set[str]] = {key: set() for key in exercises}
    harder: dict[str, set[str]] = {key: set() for key in exercises}

    def link(low: str, high: str) -> None:
        """``low`` is the easier movement, ``high`` the harder one."""
        easier[high].add(low)
        harder[low].add(high)

    for exercise in exercises.values():
        if exercise.regression_of is not None:
            link(exercise.id, exercise.regression_of)
        if exercise.progression_of is not None:
            link(exercise.progression_of, exercise.id)

    return (
        {key: tuple(sorted(value)) for key, value in easier.items()},
        {key: tuple(sorted(value)) for key, value in harder.items()},
    )


def freeze(mapping: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType(dict(mapping))
