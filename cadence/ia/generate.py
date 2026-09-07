"""Generation: prompt, call, validate, one retry, preview. Nothing is stored here.

A preview is not a workout. It is a document that has passed every gate *once*, held in a browser
until somebody looks at it. Accepting it runs the whole pipeline again from the posted bytes
(``accept`` in ``cadence/api/generate.py``), which is why there is no server-side preview cache in
this module and must never be one: a cache is what turns "re-validate" into "trust the client" the
first time someone optimises it (PRP-08 risk 6).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from cadence.bibliotheque import import_service, untrusted
from cadence.bibliotheque.import_service import ImportTarget, Prepared, strip_code_fence
from cadence.bibliotheque.untrusted import Finding
from cadence.config import Settings
from cadence.ia import client, prompt
from cadence.profils.settings import ProgramSettings
from cadence.schema.enums import AgeBand, GoalType
from cadence.schema.exercise import Exercise
from cadence.schema.youth import YouthRuleSet

logger = logging.getLogger(__name__)

# One retry and only one. A gateway that is failing consistently must not be hammered, and a model
# that got it wrong twice with the errors in front of it will not get it right on the third try.
MAX_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class GenerationOutcome:
    """What ``generate`` produced: a preview, or the reasons there is none."""

    ok: bool
    prepared: Prepared | None
    attempts: int
    model: str
    errors: tuple[Finding, ...] = ()

    def as_errors(self) -> list[dict[str, object]]:
        return [item.as_dict() for item in self.errors]


def goal_is_allowed(goal: GoalType, *, profile_kind: str, rules: YouthRuleSet | None) -> bool:
    """Principles section 3.5, read from the band table rather than from a list in code.

    A youth profile with no readable rule table is refused the body-composition goals anyway: the
    fallback is the strict reading, because a gate that cannot check must not pass (D-030).
    """
    if profile_kind != "youth":
        return True
    if rules is None:
        return goal not in (GoalType.WEIGHT, GoalType.BODY_FAT, GoalType.APPEARANCE)
    return goal not in rules.banned_goal_types


def build_prompt(
    *,
    target: ImportTarget,
    settings: ProgramSettings,
    goal: GoalType,
    gap_name: str | None,
    catalog: Mapping[str, Exercise],
    rules: YouthRuleSet | None,
) -> str:
    """The outgoing text. Every value in it comes from ``prompt.build_facts`` and nowhere else."""
    facts = prompt.build_facts(
        profile_kind=target.profile_kind,
        age_band=target.age_band,
        settings=settings,
        goal=goal,
        gap_name=gap_name,
        catalog=catalog,
        bodyweight_kg=target.bodyweight_kg,
        rules=rules,
    )
    return prompt.render(facts)


async def generate(
    *,
    target: ImportTarget,
    settings: ProgramSettings,
    config: Settings,
    goal: GoalType,
    gap_name: str | None,
    catalog: Mapping[str, Exercise],
    # What the reply is validated against, and what the prompt may offer, are not the same
    # list: a generated workout may legitimately name an imported exercise, but the prompt
    # only ever offers seed ids (D-188). Defaults to ``catalog`` so a caller cannot get the
    # looser list by forgetting the argument - it has to ask for it.
    prompt_catalog: Mapping[str, Exercise] | None = None,
    youth_rules: Mapping[AgeBand, YouthRuleSet] | None,
    rules: YouthRuleSet | None,
    taken_workout_ids: frozenset[str] = frozenset(),
    taken_exercise_ids: frozenset[str] = frozenset(),
    transport: httpx.AsyncBaseTransport | None = None,
) -> GenerationOutcome:
    """Ask for a workout, check it, and ask once more with the errors if it failed.

    Raises ``client.OmniRouteError`` when the gateway itself failed; a document that came back and
    did not pass is not an error, it is an outcome with a list of reasons.
    """
    base = build_prompt(
        target=target,
        settings=settings,
        goal=goal,
        gap_name=gap_name,
        catalog=catalog if prompt_catalog is None else prompt_catalog,
        rules=rules,
    )
    errors: tuple[Finding, ...] = ()
    model = config.omniroute_model_generate

    for attempt in range(1, MAX_ATTEMPTS + 1):
        text = prompt.with_errors(base, tuple(item.code for item in errors)) if errors else base
        completion = await client.complete(text, settings=config, transport=transport)
        model = completion.model
        prepared = import_service.prepare(
            strip_code_fence(completion.text),
            target=target,
            catalog=catalog,
            youth_rules=youth_rules,
            # The workout ids are deliberately not passed: a generated document that lands on a
            # taken id is renamed below rather than refused, and the *accept* path checks the
            # collision for real against the ids as they are at that moment.
            taken_workout_ids=frozenset(),
            taken_exercise_ids=taken_exercise_ids,
        )
        if prepared.ok:
            logger.info("generation succeeded on attempt %d with model %s", attempt, model)
            return GenerationOutcome(
                ok=True,
                prepared=_with_free_id(prepared, taken_workout_ids),
                attempts=attempt,
                model=model,
            )
        errors = prepared.errors
        logger.info("generation attempt %d failed %d checks", attempt, len(errors))

    return GenerationOutcome(ok=False, prepared=None, attempts=MAX_ATTEMPTS, model=model, errors=errors)


def _with_free_id(prepared: Prepared, taken: frozenset[str]) -> Prepared:
    """Rename a generated workout onto a free id rather than letting it overwrite one.

    An import keeps the id it was given and is refused on a collision, because a person who typed
    that id meant it. A generated id is a slug a model invented, so a numeric suffix is a repair
    rather than a surprise - and a seed row is never replaced either way (PRP-08 risk 7).
    """
    workout = prepared.workout
    if workout is None or workout.id not in taken:  # pragma: no branch - the common path
        return prepared
    free = import_service.free_workout_id(workout.id, taken)
    document = {**dict(prepared.document), "id": free}
    return Prepared(
        ok=True,
        errors=prepared.errors,
        warnings=prepared.warnings,
        ignored_keys=prepared.ignored_keys,
        workout=workout.model_copy(update={"id": free}),
        inline_exercises=prepared.inline_exercises,
        document=document,
    )


def unavailable_finding() -> Finding:
    """The one message a household with no key ever sees. It names no variable and no value."""
    return Finding(
        untrusted.GENERATION_UNAVAILABLE,
        "workout generation is not set up on this install, so there is nothing to ask. Import a workout instead.",
    )


__all__ = [
    "MAX_ATTEMPTS",
    "GenerationOutcome",
    "build_prompt",
    "generate",
    "goal_is_allowed",
    "unavailable_finding",
]
