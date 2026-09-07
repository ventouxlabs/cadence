"""Building the generation prompt out of a **closed** set of placeholders.

The brief's hard rule is that health data leaves the homelab only via OmniRoute and only as the
fields a prompt needs. That is enforced here by construction rather than by care: ``PLACEHOLDERS``
is the whole vocabulary the template may interpolate, ``render`` refuses a template that mentions
anything else, and no function in this module is handed a session, a metric or a profile row.

The band is the only age-derived value that leaves the machine. The exact age, the display name,
the VitalForge slug, the bodyweight and every token stay on this side of the call.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cadence.profils.settings import ProgramSettings
from cadence.programme.ladder import parse_weights_available
from cadence.schema.enums import AgeBand, Equipment, GoalType
from cadence.schema.exercise import Exercise
from cadence.schema.youth import YouthRuleSet
from cadence.validateur import codes
from cadence.validateur.youth_rules import youth_allows

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "generate.md"

# The whole vocabulary. Adding a name here is the one place a new fact can start travelling, so
# it is the one place a reviewer has to look.
PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "profile_kind",
        "age_band",
        "equipment_ids",
        "weight_ranges",
        "days_per_week",
        "session_minutes",
        "goal",
        "gap_name",
        "allowed_exercises",
        "youth_rules",
        "schema_example",
    }
)

_TOKEN = re.compile(r"\[\[([a-z_]+)\]\]")

RETRY_HEADING = "Previous attempt failed these checks:"

# Plain-language goal labels (D-026). The enum is PRP-00's and never changes; only the wording a
# child reads does, which is why "appearance" is never spelled out on a youth screen.
GOAL_LABELS: dict[GoalType, str] = {
    GoalType.STRENGTH: "Get stronger",
    GoalType.POSTURE: "Better posture",
    GoalType.MOVEMENT_QUALITY: "Move better and play",
    GoalType.CONSISTENCY: "Keep the habit",
    GoalType.WEIGHT: "Weight (adult only)",
    # D-027 bans the phrase "body fat" from the son's screens; the Settings card is a
    # parent screen, but a label costs nothing to word so the phrase exists nowhere.
    GoalType.BODY_FAT: "Fat loss (adult only)",
    GoalType.APPEARANCE: "Look better (adult only)",
}

# The shape of a reply, filled in with the kind of profile actually being generated for.
#
# ``target_profile_kind`` was a literal ``adult`` here, in the one line of the prompt a model is
# most likely to copy verbatim. Generating for the son, that example invited the exact document
# the validator then refused with ``profile_kind_mismatch``, and the retry re-sent the same
# example. The field is filled from the real target instead, so the example can only ever
# demonstrate a value that will pass (D-189).
#
# ``rpe_target`` is shown on the loaded row because the youth rule table talks about effort caps
# without the field ever appearing here, and the schema is ``extra="forbid"``: a model reaching
# for the obvious ``rpe`` or ``effort`` gets a fatal unknown-key error instead of a capped row.
_SCHEMA_EXAMPLE = """id: posture-focus
name: Posture focus
day_type: upper_a
target_profile_kind: {profile_kind}
estimated_minutes: 30
notes: optional, one short line
rows:
  - exercise_id: wall-angel
    sets: 2
    reps: 10
    load_unit: bodyweight
    rest_s: 45
    cue_override: optional, one short line
  - exercise_id: db-bent-row
    sets: 3
    reps: 10
    load_kg: 9.0
    load_unit: per_hand
    rest_s: 60
    rpe_target: 7
"""


def schema_example(profile_kind: str) -> str:
    """The example reply, targeting the profile this generation is actually for."""
    return _SCHEMA_EXAMPLE.format(profile_kind=profile_kind).strip()


NO_GAP = "nothing in particular"


@dataclass(frozen=True, slots=True)
class PromptFacts:
    """The rendered values, kept separately so a test can assert on them one at a time."""

    values: Mapping[str, str]

    def get(self, name: str) -> str:
        return self.values[name]


def goal_label(goal: GoalType) -> str:
    return GOAL_LABELS.get(goal, goal.value.replace("_", " ").capitalize())


def allowed_exercise_ids(
    catalog: Mapping[str, Exercise],
    *,
    equipment: tuple[Equipment, ...],
    profile_kind: str,
    age_band: AgeBand,
    bodyweight_kg: float | None,
    rules: YouthRuleSet | None,
) -> tuple[str, ...]:
    """The ids a workout for this profile could legally name, in a stable order.

    Filtered by the same facts the validator will judge against, so the model is not invited to
    prescribe a movement that would then be rejected. It is a courtesy to the model and never a
    substitute for the gate: ``validate_workout`` runs on whatever comes back regardless.

    ``bodyweight_kg`` reaches this function and never the prompt. It decides whether a
    ``conditional`` movement is offered; the number itself does not travel.
    """
    available = set(equipment)
    allowed: list[str] = []
    for key in sorted(catalog):
        exercise = catalog[key]
        if not available.issuperset(exercise.equipment):
            continue
        if profile_kind == "youth":
            if rules is not None and exercise.load_type not in rules.allowed_load_types:
                continue
            if not youth_allows(exercise, age_band, bodyweight_kg=bodyweight_kg):
                continue
            if rules is not None and set(exercise.tags) & set(rules.banned_tags):
                continue
        allowed.append(f"- {exercise.id} ({exercise.pattern.value}, {exercise.load_type.value})")
    return tuple(allowed)


def youth_rule_lines(profile_kind: str, rules: YouthRuleSet | None) -> tuple[str, ...]:
    """The band's caps as a compact list, or the adult note when there is no youth table."""
    if profile_kind != "youth" or rules is None:
        return ("- Adult rules: no youth load caps. Keep every row at a hard but repeatable effort.",)
    lines = [
        f"- Allowed load types: {', '.join(item.value for item in rules.allowed_load_types)}",
        f"- Reps on a loaded row: {rules.rep_min_loaded} to {rules.rep_max_loaded}",
        f"- Reps on a bodyweight row: {rules.rep_min_bodyweight} to {rules.rep_max_bodyweight}",
        f"- Rest after a loaded row: at least {rules.min_rest_s_loaded} seconds",
        f"- Sets per exercise: at most {rules.max_sets_per_exercise}",
        f"- Exercises in the session: at most {rules.max_exercises_per_session}",
        f"- Session length: at most {rules.max_session_minutes} minutes",
        f"- Effort: never above RPE {rules.rpe_cap}",
        "- No AMRAP, no sets to failure, no max-effort or one-rep-max work.",
    ]
    if rules.max_load_kg_per_hand is not None:
        lines.append(f"- Load per hand: at most {rules.max_load_kg_per_hand} kg")
    if rules.max_load_kg_per_implement is not None:
        lines.append(f"- Load per implement: at most {rules.max_load_kg_per_implement} kg")
    return tuple(lines)


def weight_range_text(settings: ProgramSettings) -> str:
    """The rack, described from PRP-01's parser rather than from the raw free-text setting.

    The stored string is something a person typed; sending the parsed ladders sends the numbers
    and nothing else, so a note somebody added to that box never travels.
    """
    parsed = parse_weights_available(settings.weights_available)
    parts: list[str] = []
    for load_type, rungs in sorted(parsed.ladders.items(), key=lambda item: item[0].value):
        if not rungs:
            continue
        ordered = sorted(rungs)
        if len(ordered) > 3:
            parts.append(f"{load_type.value} {ordered[0]:g}-{ordered[-1]:g} kg, {len(ordered)} steps")
        else:
            parts.append(f"{load_type.value} {', '.join(f'{rung:g}' for rung in ordered)} kg")
    if parsed.bench:
        parts.append("adjustable bench")
    return "; ".join(parts) or "bodyweight only"


def build_facts(
    *,
    profile_kind: str,
    age_band: AgeBand,
    settings: ProgramSettings,
    goal: GoalType,
    gap_name: str | None,
    catalog: Mapping[str, Exercise],
    bodyweight_kg: float | None,
    rules: YouthRuleSet | None,
) -> PromptFacts:
    """Every value the template may see, and nothing else."""
    allowed = allowed_exercise_ids(
        catalog,
        equipment=tuple(settings.equipment),
        profile_kind=profile_kind,
        age_band=age_band,
        bodyweight_kg=bodyweight_kg,
        rules=rules,
    )
    values = {
        "profile_kind": profile_kind,
        "age_band": age_band.value,
        "equipment_ids": ", ".join(item.value for item in settings.equipment),
        "weight_ranges": weight_range_text(settings),
        "days_per_week": str(settings.days_per_week),
        "session_minutes": str(settings.session_minutes),
        "goal": goal.value,
        "gap_name": gap_name or NO_GAP,
        "allowed_exercises": "\n".join(allowed) or "- (none available with this equipment)",
        "youth_rules": "\n".join(youth_rule_lines(profile_kind, rules)),
        "schema_example": schema_example(profile_kind),
    }
    return PromptFacts(values=values)


# What leaves the house in one request. The template plus a full seed library is ~6 KB, so this
# is four times the real thing and still small enough that a gateway cannot be used to exfiltrate
# anything at volume (Codex finding 7).
MAX_PROMPT_BYTES = 24 * 1024


class PromptTooLarge(RuntimeError):
    """The prompt would not fit the byte cap even with the exercise list emptied."""


def _fit(text: str, facts: PromptFacts, template: str | None) -> str:
    """Re-render with fewer exercises offered until the prompt fits, or refuse.

    The exercise list is the only part that grows with the data; everything else is the template,
    the band table and eleven short values. Trimming from the end keeps the list in its stable
    id order, so two runs against the same library produce the same prompt.
    """
    listed = facts.get("allowed_exercises").splitlines()
    while listed and len(text.encode("utf-8")) > MAX_PROMPT_BYTES:
        listed = listed[: len(listed) - max(1, len(listed) // 8)]
        trimmed = PromptFacts(values={**dict(facts.values), "allowed_exercises": "\n".join(listed)})
        text = _interpolate(trimmed, template)
    if len(text.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise PromptTooLarge(f"the prompt is over {MAX_PROMPT_BYTES // 1024} KB with nothing left to trim")
    return text


def _interpolate(facts: PromptFacts, template: str | None) -> str:
    text = template if template is not None else PROMPT_PATH.read_text(encoding="utf-8")
    unknown = set(_TOKEN.findall(text)) - PLACEHOLDERS
    if unknown:
        raise ValueError(f"the prompt template names placeholders that are not allowed: {', '.join(sorted(unknown))}")
    return _TOKEN.sub(lambda match: facts.get(match.group(1)), text)


def render(facts: PromptFacts, *, template: str | None = None) -> str:
    """Fill the template. A placeholder outside ``PLACEHOLDERS`` is a bug, not a blank.

    Refusing rather than substituting an empty string is deliberate: a typo in a placeholder name
    would otherwise ship a prompt with a silently missing rule table, and a youth workout would be
    generated against no caps at all.
    """
    return _fit(_interpolate(facts, template), facts, template)


# What a failed check is *called* on the retry, and what to do about it. Codes and static text
# only: the validator's own messages quote the value that broke the rule, and for a youth profile
# several of those values are bodyweight-derived - "16.0 kg per_implement is over the 14 kg cap"
# tells a remote model the child weighs 40 kg (D-184). The remedies come from PRP-00's own
# ``REMEDY`` table wherever it is specific enough to act on; the overrides below replace the
# entries that are too terse to be useful to a model, or that name an internal file path.
RETRY_GUIDANCE: dict[str, str] = {
    codes.YOUTH_LOAD_EXCEEDED: "the load on that row is above this band's cap; use a lighter implement or bodyweight",
    codes.YOUTH_REP_CEILING: "lower the reps on that row to the top of the band's range",
    codes.YOUTH_REP_FLOOR: "raise the reps on a loaded row to the bottom of the band's range",
    codes.YOUTH_REST_FLOOR: "give a loaded row at least the band's minimum rest",
    codes.YOUTH_RULES_UNAVAILABLE: "this profile's rules could not be read; prescribe bodyweight movements only",
    codes.REQUIRES_ANCHOR_UNAVAILABLE: "there is no pull-up bar here; use a movement that needs no overhead anchor",
    codes.YOUTH_BANNED_GOAL: "that goal is not one this profile trains for",
    codes.BODYWEIGHT_INVALID: "do not prescribe anything that depends on a recorded bodyweight",
    codes.YOUTH_RPE_EXCEEDED: "lower the effort target on that row",
}

# The generic line for a code with no guidance anywhere. Never the validator's message.
UNNAMED_PROBLEM = "that row breaks a rule for this profile; choose a different movement or prescription"


def retry_lines(error_codes: tuple[str, ...]) -> tuple[str, ...]:
    """One line per distinct failed check, in first-seen order, naming the code and what to do."""
    lines: list[str] = []
    for code in dict.fromkeys(error_codes):
        guidance = RETRY_GUIDANCE.get(code) or codes.REMEDY.get(code) or UNNAMED_PROBLEM
        lines.append(f"- {code}: {guidance}")
    return tuple(lines)


def with_errors(prompt: str, error_codes: tuple[str, ...]) -> str:
    """The retry prompt: the same text with the failed **codes** appended, once.

    Codes, never the validator's messages. A message quotes the offending value, and on a youth
    profile the cap it is compared against is computed from the child's bodyweight - so forwarding
    it would send a number the prompt is specifically built never to send.
    """
    if not error_codes:
        return prompt
    listed = "\n".join(retry_lines(error_codes))
    return f"{prompt}\n\n## {RETRY_HEADING}\n\n{listed}\n\nFix all of them and answer again with one YAML document."


__all__ = [
    "GOAL_LABELS",
    "MAX_PROMPT_BYTES",
    "PLACEHOLDERS",
    "PromptTooLarge",
    "RETRY_GUIDANCE",
    "RETRY_HEADING",
    "PromptFacts",
    "allowed_exercise_ids",
    "build_facts",
    "goal_label",
    "render",
    "retry_lines",
    "weight_range_text",
    "with_errors",
    "youth_rule_lines",
]
