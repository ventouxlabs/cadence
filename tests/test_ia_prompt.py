"""What actually leaves the homelab: the generation prompt - PRP-08 tests 28, 29.

The brief's hard rule is that health data leaves only via OmniRoute and only as the fields a
prompt needs, so these assertions are made on the **captured request body**, not on the function
that builds it. Every one of them carries a positive assertion too, so none of them can pass by
the prompt being empty.
"""

from __future__ import annotations

import re

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from cadence.ia import client, prompt
from cadence.ia import generate as ia
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile
from tests import documents as doc
from tests.conftest import OmniRouteGateway

KEY = "or-live-DO-NOT-LEAK-1d4e7f0a"


@pytest.fixture
def described_family(db_session, seeded: FastAPI) -> FastAPI:
    """A household with facts worth leaking: a name, a slug, an age and a bodyweight."""
    son = db_session.get(Profile, PROFILE_SON)
    son.age_years = 12
    son.age_band = "age_10_13"
    son.bodyweight_kg = 41.0
    son.display_name = "Theodore"
    son.vitalforge_person = "theo-slug"
    me = db_session.get(Profile, PROFILE_ME)
    me.display_name = "Jean-Dominique"
    me.vitalforge_person = "jd-slug"
    db_session.add(son)
    db_session.add(me)
    db_session.commit()
    return seeded


# ---------------------------------------------------------------------------------- the prompt


async def test_prompt_contains_no_secrets_or_logs(
    described_family: FastAPI, omniroute_gateway: OmniRouteGateway, settings, monkeypatch
) -> None:
    """Test 28: the outgoing body carries the band and nothing else about this family."""
    from cadence.config import get_settings as config_settings

    keyed = settings.model_copy(update={"omniroute_key": SecretStr(KEY)})
    described_family.dependency_overrides[config_settings] = lambda: keyed
    described_family.state.settings = keyed
    monkeypatch.setattr(client, "TRANSPORT", omniroute_gateway.transport)
    omniroute_gateway.reply(doc.GENERATED_YOUTH)

    transport = httpx.ASGITransport(app=described_family)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.post("/api/generate", json={"profile": "son", "goal": "posture"})
    assert response.status_code == 200, response.text

    body = omniroute_gateway.requests[0].content.decode()
    for forbidden in ("Theodore", "Jean-Dominique", "theo-slug", "jd-slug", "OMNIROUTE", "VITALFORGE", "Bearer"):
        assert forbidden not in body, forbidden
    # Non-vacuity: the one age-derived value that is *supposed* to travel does.
    assert "age_10_13" in body


async def test_prompt_contains_band_not_age(
    described_family: FastAPI, omniroute_gateway: OmniRouteGateway, settings, monkeypatch
) -> None:
    """Test 29: a twelve-year-old's prompt says ``age_10_13`` and never ``12``."""
    from cadence.config import get_settings as config_settings

    keyed = settings.model_copy(update={"omniroute_key": SecretStr(KEY)})
    described_family.dependency_overrides[config_settings] = lambda: keyed
    described_family.state.settings = keyed
    monkeypatch.setattr(client, "TRANSPORT", omniroute_gateway.transport)
    omniroute_gateway.reply(doc.GENERATED_YOUTH)

    transport = httpx.ASGITransport(app=described_family)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        await http.post("/api/generate", json={"profile": "son", "goal": "posture"})

    sent = omniroute_gateway.prompts[0]
    assert "age_10_13" in sent
    assert re.search(r"(?<![\d_])12(?![\d_])", sent) is None, "the exact age reached the prompt"
    assert re.search(r"(?<![\d.])41(?![\d.])", sent) is None, "the bodyweight reached the prompt"


def test_the_prompt_template_names_only_allowed_placeholders() -> None:
    """The closed vocabulary is checked against the shipped file, not against a copy in a test."""
    text = prompt.PROMPT_PATH.read_text(encoding="utf-8")
    named = set(re.findall(r"\[\[([a-z_]+)\]\]", text))
    assert named
    assert named <= prompt.PLACEHOLDERS


def test_render_refuses_an_unknown_placeholder() -> None:
    """A typo would otherwise ship a prompt with the youth rule table silently missing."""
    facts = prompt.PromptFacts(values=dict.fromkeys(prompt.PLACEHOLDERS, "x"))
    with pytest.raises(ValueError, match="not allowed"):
        prompt.render(facts, template="hello [[bodyweight_kg]]")


def test_youth_goals_never_mention_a_body(youth_rules) -> None:
    """Principles section 3.5, from the shipped band table rather than from a constant here."""
    from cadence.schema.enums import AgeBand, GoalType

    for band in (AgeBand.U10, AgeBand.AGE_10_13, AgeBand.AGE_14_17):
        rules = youth_rules[band]
        for goal in (GoalType.WEIGHT, GoalType.BODY_FAT, GoalType.APPEARANCE):
            assert not ia.goal_is_allowed(goal, profile_kind="youth", rules=rules)
        assert ia.goal_is_allowed(GoalType.POSTURE, profile_kind="youth", rules=rules)


def test_a_youth_with_no_rule_table_still_loses_the_body_goals() -> None:
    """A gate that cannot read its table refuses rather than passes (D-030)."""
    from cadence.schema.enums import GoalType

    assert not ia.goal_is_allowed(GoalType.APPEARANCE, profile_kind="youth", rules=None)


async def test_the_schema_example_targets_the_profile_being_generated_for(
    described_family: FastAPI, omniroute_gateway: OmniRouteGateway, settings, monkeypatch
) -> None:
    """The example reply carries the son's kind, not a literal ``adult`` (D-189).

    The example is the one line of the prompt a model is most likely to copy verbatim, so a
    hardcoded ``adult`` invited exactly the document ``validate_workout`` then refused with
    ``profile_kind_mismatch`` — and the retry re-sent the same example.
    """
    from cadence.config import get_settings as config_settings

    keyed = settings.model_copy(update={"omniroute_key": SecretStr(KEY)})
    described_family.dependency_overrides[config_settings] = lambda: keyed
    described_family.state.settings = keyed
    monkeypatch.setattr(client, "TRANSPORT", omniroute_gateway.transport)
    omniroute_gateway.reply(doc.GENERATED_YOUTH)

    transport = httpx.ASGITransport(app=described_family)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        await http.post("/api/generate", json={"profile": PROFILE_SON, "goal": "posture"})

    sent = omniroute_gateway.prompts[0]
    assert "target_profile_kind: youth" in sent
    assert "target_profile_kind: adult" not in sent
    # The example also shows ``rpe_target``, which the youth rule lines talk about in words while
    # the schema forbids unknown keys: a model inventing ``rpe`` gets a fatal error instead.
    assert "rpe_target: 7" in sent


def test_the_schema_example_names_the_kind_it_is_given() -> None:
    """Directly, so the interpolation is covered without a gateway in the way."""
    assert "target_profile_kind: adult" in prompt.schema_example("adult")
    assert "target_profile_kind: youth" in prompt.schema_example("youth")
