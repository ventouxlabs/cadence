"""Solo mode: hiding the son (D-260..D-266).

The feature is a *display* setting, so every test here is really one of two questions. Which
surfaces stop offering him — the tab strip, the screens, the profile-addressed API reads. And,
much more importantly, what survives: his profile row, his program, his planned days, the
sessions he has already finished, his assessments and his challenges. ``test_the_round_trip_...``
is the one that would have to fail before any of this shipped.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from sqlmodel import Session as DbSession
from sqlmodel import select

from cadence.bilan.tables import Assessment, Challenge
from cadence.profils.settings import DEFAULT_SETTINGS, ProgramSettings
from cadence.profils.tables import PROFILE_ME, PROFILE_SON, Profile, Setting
from cadence.profils.visibility import ALL_PROFILE_TABS, is_hidden, son_enabled, visible_profile_tabs
from cadence.programme.tables import PlannedSession, Program
from cadence.seance.tables import SessionRecord, SessionRowRecord

SEE_OTHER = 303
NOT_FOUND = 404

# The screens that carry a tab strip and take ``?profile=``. ``/done/{id}`` is exercised on its
# own below, because it needs a real session id to address.
SCREENS: tuple[str, ...] = ("/today", "/history", "/assess")

HIDDEN_KEYS: tuple[str, ...] = ("son", "together")


def _set_son_enabled(db: DbSession, value: bool) -> None:
    """Write the setting directly, the way a household that has already saved the form has it."""
    row = db.get(Setting, "son_enabled")
    assert row is not None, "seeding must write every key in SETTING_KEYS"
    row.value_json = json.dumps(value)
    db.add(row)
    db.commit()


# ------------------------------------------------------------------ the rule itself


def test_the_default_is_on_so_an_untouched_install_is_unchanged() -> None:
    """A household that never opens the toggle behaves exactly as it did before it existed."""
    assert DEFAULT_SETTINGS.son_enabled is True
    assert son_enabled(DEFAULT_SETTINGS) is True
    assert visible_profile_tabs(DEFAULT_SETTINGS) == ALL_PROFILE_TABS


def test_a_database_written_before_the_setting_existed_reads_as_on() -> None:
    """The key is absent from every row an older build wrote; absent must mean the son is shown."""
    from cadence.profils.settings import settings_from_rows

    older = {"days_per_week": "4", "setup_complete": "true"}
    assert settings_from_rows(older).son_enabled is True


def test_solo_leaves_one_tab_and_it_is_the_parent() -> None:
    solo = DEFAULT_SETTINGS.with_changes(son_enabled=False)
    assert visible_profile_tabs(solo) == ((PROFILE_ME, "Me"),)


@pytest.mark.parametrize("key", [*HIDDEN_KEYS, "SON", " together "])
def test_the_hidden_keys_are_the_son_and_the_shared_tab(key: str) -> None:
    """Case and space are normalised first: ``?profile=SON`` hides for the same reason ``son`` does."""
    solo = DEFAULT_SETTINGS.with_changes(son_enabled=False)
    assert is_hidden(solo, key) is True
    assert is_hidden(DEFAULT_SETTINGS, key) is False


@pytest.mark.parametrize("key", [None, "me", "", "  ", "nobody", "../etc/passwd"])
def test_a_key_that_is_not_the_son_is_never_hidden(key: str | None) -> None:
    """An unknown profile stays unknown. Hiding it would route a typo to the parent's screen."""
    assert is_hidden(DEFAULT_SETTINGS.with_changes(son_enabled=False), key) is False


# ------------------------------------------------------------------ the tab strip


async def test_the_tab_strip_offers_all_three_while_the_son_is_shown(seeded_client: httpx.AsyncClient) -> None:
    body = (await seeded_client.get("/today?profile=me")).text
    for key in ("me", *HIDDEN_KEYS):
        assert f'href="/today?profile={key}"' in body, key


@pytest.mark.parametrize("path", ["/today?profile=me", "/history?profile=me", "/settings"])
async def test_no_screen_offers_a_tab_for_a_hidden_profile(
    seeded_client: httpx.AsyncClient, db_session: DbSession, path: str
) -> None:
    """Not a disabled link and not a link that only redirects back: the tab is not rendered."""
    _set_son_enabled(db_session, False)
    body = (await seeded_client.get(path)).text
    assert 'profile=me"' in body
    for key in HIDDEN_KEYS:
        assert f"profile={key}" not in body, key


# ------------------------------------------------------------------ the screens redirect


@pytest.mark.parametrize("screen", SCREENS)
@pytest.mark.parametrize("key", HIDDEN_KEYS)
async def test_a_screen_asked_for_a_hidden_profile_lands_on_the_parents_tab(
    seeded_client: httpx.AsyncClient, db_session: DbSession, screen: str, key: str
) -> None:
    """A stale bookmark or a cached PWA page is a soft landing, never a 404 (D-260)."""
    _set_son_enabled(db_session, False)
    response = await seeded_client.get(f"{screen}?profile={key}")
    assert response.status_code == SEE_OTHER
    assert response.headers["location"] == f"{screen}?profile=me"


@pytest.mark.parametrize("query", ["", "?profile=me", "?profile=son", "?profile=together"])
async def test_the_done_screen_is_refused_however_the_tab_is_written(
    seeded_client: httpx.AsyncClient, db_session: DbSession, query: str
) -> None:
    """D-271. The screen belongs to whoever owns the session, not to whatever the query says.

    ``?profile=me`` and **no query at all** are the cases that mattered and the ones a tab check
    could not see: ``app.js`` navigates to a bare ``/done/{id}`` when it lands a queued Done, so
    before this the son's summary rendered there in full youth scope on a household that had
    hidden him. Today rather than this path on another tab, because this path *is* his screen
    however the query reads - rewriting the query would bounce straight back here.
    """
    session_id = (await seeded_client.get("/api/today?profile=son")).json()["data"]["session_id"]
    _set_son_enabled(db_session, False)

    # Followed rather than inspected one hop at a time. ``?profile=son`` bounces twice - the tab
    # check rewrites the query, then the owner check sends it on to Today - and it is where the
    # person ends up that the promise is about, not how many 303s it took to get there.
    response = await seeded_client.get(f"/done/{session_id}{query}", follow_redirects=True)
    assert response.status_code == 200
    assert response.url.path == "/today"
    assert response.url.params.get("profile") == PROFILE_ME

    # And nothing of his leaked on the way: no hop rendered the summary or its youth scope.
    for hop in [*response.history, response]:
        assert session_id not in hop.text
        assert "data-profile-kind" not in hop.text


async def test_the_parents_done_screen_still_renders_from_a_bare_url(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The regression the owner check could most easily have caused.

    ``app.js`` lands a queued Done on ``/done/{id}`` with no ``?profile=``, so a check that
    required the query - or that resolved the tab to ``me`` and then demanded the session match
    it - would have 404'd the offline finish for everybody, hidden son or not.
    """
    data = (await seeded_client.get("/api/today?profile=me")).json()["data"]
    session_id = data["session_id"]
    await seeded_client.post(f"/api/sessions/{session_id}/done", json={"felt": "right"})
    _set_son_enabled(db_session, False)

    assert (await seeded_client.get(f"/done/{session_id}")).status_code == 200


async def test_the_sons_done_screen_is_untouched_while_he_is_shown(seeded_client: httpx.AsyncClient) -> None:
    """The other half: the owner check must do nothing at all on a household showing both."""
    session_id = (await seeded_client.get("/api/today?profile=son")).json()["data"]["session_id"]
    for query in ("", "?profile=son", "?profile=me"):
        assert (await seeded_client.get(f"/done/{session_id}{query}")).status_code == 200, query


@pytest.mark.parametrize("screen", SCREENS)
async def test_the_parents_own_screens_are_untouched(
    seeded_client: httpx.AsyncClient, db_session: DbSession, screen: str
) -> None:
    _set_son_enabled(db_session, False)
    assert (await seeded_client.get(f"{screen}?profile=me")).status_code == 200


@pytest.mark.parametrize("screen", SCREENS)
async def test_nothing_redirects_while_the_son_is_shown(seeded_client: httpx.AsyncClient, screen: str) -> None:
    """The control group. With the son shown, both of his tabs render.

    ``/assess?profile=together`` is the one exception, and it predates this feature: people are
    tested alone, so that screen has always bounced the shared tab to the parent (``_resolve``).
    Solo mode sends it to the same place for a different reason, which is why the redirect test
    above passes for it either way.
    """
    for key in HIDDEN_KEYS:
        if (screen, key) == ("/assess", "together"):
            continue
        assert (await seeded_client.get(f"{screen}?profile={key}")).status_code == 200, key


# ------------------------------------------------------------------ the JSON API refuses


@pytest.mark.parametrize("path", ["/api/today", "/api/sessions", "/api/scorecard", "/api/assessments"])
@pytest.mark.parametrize("key", HIDDEN_KEYS)
async def test_a_profile_addressed_read_is_a_404_not_somebody_elses_data(
    seeded_client: httpx.AsyncClient, db_session: DbSession, path: str, key: str
) -> None:
    """An API is not a screen. Substituting the parent silently would be a lie under a 200."""
    _set_son_enabled(db_session, False)
    response = await seeded_client.get(f"{path}?profile={key}")
    assert response.status_code == NOT_FOUND, path
    body = response.json()
    assert body["error"]
    assert body.get("data") is None


async def test_the_metrics_read_is_refused_by_profile_id(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """``/api/metrics`` is addressed by profile id and has no ``together``; only the son is hidden."""
    _set_son_enabled(db_session, False)
    assert (await seeded_client.get("/api/metrics?profile=son")).status_code == NOT_FOUND
    assert (await seeded_client.get("/api/metrics?profile=me")).status_code == 200


async def test_the_profile_read_is_refused_but_the_writes_stay_open(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """``GET /api/profiles/son`` is a read addressed by profile, so it goes with the others.

    The two ``PUT``s on the same path are writes and must stay reachable (D-264): a hidden profile
    is still a profile somebody may need to correct before showing him again.
    """
    _set_son_enabled(db_session, False)
    assert (await seeded_client.get("/api/profiles/son")).status_code == NOT_FOUND
    assert (await seeded_client.get("/api/profiles/me")).status_code == 200

    written = await seeded_client.put("/api/profiles/son", json={"vitalforge_person": "kid"})
    assert written.status_code == 200, written.text
    db_session.expire_all()
    assert db_session.get(Profile, PROFILE_SON).vitalforge_person == "kid"


async def test_an_htmx_fragment_asks_for_a_navigation_rather_than_swapping_a_whole_page(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-269. A 303 here would post a full ``<!DOCTYPE html>`` into a one-line caption slot."""
    _set_son_enabled(db_session, False)
    response = await seeded_client.get("/history?profile=son", headers={"hx-request": "true"})
    assert response.status_code == 204
    assert response.headers["hx-redirect"] == "/history?profile=me"
    assert not response.content


@pytest.mark.parametrize(
    "path",
    ["/history/sessions/whatever-id", "/history/badges/son/first-session"],
)
async def test_a_fragment_lands_on_its_parent_screen_and_not_on_its_own_404(
    seeded_client: httpx.AsyncClient, db_session: DbSession, path: str
) -> None:
    """D-272. Rewriting only the query sent these to a fragment the parent's tab does not own.

    That is a 404 page — the hard landing D-260 exists to prevent — reached from a link the app
    itself rendered. A fragment has a screen it belongs to, and that is where its bounce goes.
    """
    _set_son_enabled(db_session, False)
    response = await seeded_client.get(f"{path}?profile=son")
    assert response.status_code == SEE_OTHER
    assert response.headers["location"] == "/history?profile=me"


async def test_a_history_card_reads_its_tab_from_the_query_and_never_from_the_session(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-268, which is D-249 applied to the route that missed it the first time.

    Resolving the tab from ``summary.profile_id`` when the query was absent let the request
    authorise itself from the very session it was checking, so the son's card rendered on a
    household that had hidden him.
    """
    data = (await seeded_client.get("/api/today?profile=son")).json()["data"]
    session_id = data["session_id"]
    for row in data["rows"]:
        await seeded_client.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"done": True})
    await seeded_client.post(f"/api/sessions/{session_id}/done", json={"felt": "right"})
    _set_son_enabled(db_session, False)

    bare = await seeded_client.get(f"/history/sessions/{session_id}", headers={"hx-request": "true"})
    assert bare.status_code == NOT_FOUND
    assert "goblet" not in bare.text.lower()


@pytest.mark.parametrize(
    "path",
    ["/api/today", "/api/sessions", "/api/scorecard", "/api/metrics", "/api/assessments", "/api/profiles/{key}"],
)
async def test_the_refusal_says_the_same_thing_an_absent_profile_says(
    seeded_client: httpx.AsyncClient, db_session: DbSession, path: str
) -> None:
    """Two different sentences would tell a caller which of the two states this household is in.

    Every endpoint, not just the first one (D-273): the four of them word an absent profile
    differently, so a single shared sentence in ``refuse_hidden`` made hidden and absent
    distinguishable on three of them - the side channel that function exists to close.
    """

    async def _error(key: str) -> str:
        url = path.format(key=key) if "{key}" in path else f"{path}?profile={key}"
        response = await seeded_client.get(url)
        assert response.status_code == NOT_FOUND, url
        return str(response.json()["error"])

    absent = await _error("nobody")
    _set_son_enabled(db_session, False)
    hidden = await _error("son")
    assert hidden.replace("'son'", "'nobody'") == absent, path


@pytest.mark.parametrize("path", ["/api/today", "/api/sessions", "/api/scorecard", "/api/metrics"])
async def test_the_parents_reads_are_untouched(
    seeded_client: httpx.AsyncClient, db_session: DbSession, path: str
) -> None:
    _set_son_enabled(db_session, False)
    assert (await seeded_client.get(f"{path}?profile=me")).status_code == 200, path


# ------------------------------------------------------- the offline queue still lands


async def test_a_queued_tick_for_the_sons_session_is_still_accepted(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The whole reason the 404 is scoped to profile-addressed **reads** (D-264).

    The phone's outbox holds operations keyed by session id, and ``app.js`` treats any 4xx as a
    permanent refusal that drops the operation rather than retrying it. A tick the son made on
    Sunday, queued while the phone was offline, must still land on Monday after the toggle went
    off on Sunday night - otherwise turning a display setting off destroys work, which is the one
    thing this feature promises it never does.
    """
    data = (await seeded_client.get("/api/today?profile=son")).json()["data"]
    session_id, position = data["session_id"], data["rows"][0]["position"]
    _set_son_enabled(db_session, False)

    tick = await seeded_client.post(f"/api/sessions/{session_id}/rows/{position}", json={"done": True})
    assert tick.status_code == 200, tick.text
    assert tick.json()["data"]["done"] is True

    done = await seeded_client.post(f"/api/sessions/{session_id}/done", json={"felt": "right"})
    assert done.status_code == 200, done.text


async def test_the_html_write_routes_are_not_redirected_either(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """``?profile=son`` on a tick is a hint about where to send the browser, not an address.

    A 303 here would answer the write by throwing it away, and the no-JavaScript path posts
    exactly this shape.
    """
    data = (await seeded_client.get("/api/today?profile=son")).json()["data"]
    session_id, position = data["session_id"], data["rows"][0]["position"]
    _set_son_enabled(db_session, False)

    response = await seeded_client.post(f"/today/{session_id}/rows/{position}/tick?profile=son", data={"done": "on"})
    # Back to Today, which is a redirect the *handler* chose - the guard's would have rewritten
    # the path to the tick route itself on ``?profile=me``.
    assert response.status_code == SEE_OTHER
    assert response.headers["location"].startswith("/today?profile=")

    # And the tick actually landed. Asserting the redirect alone would only prove the guard kept
    # out of the way, not that the write it was keeping out of the way of went through - and the
    # write is the whole promise. Read from the database, because his API reads are 404 now.
    db_session.expire_all()
    row = db_session.exec(
        select(SessionRowRecord)
        .where(SessionRowRecord.session_id == session_id)
        .where(SessionRowRecord.position == position)
    ).one()
    assert row.done is True


# ------------------------------------- the check-in writes, and answers with the parent's screen


def _posted_battery(page: str) -> dict[str, str]:
    """The son's own form, read back off his rendered page.

    Scraped rather than hand-written so the values are ones the screen itself offers and the
    battery actually validates: the test is about the *answer* the route gives, and a payload
    that failed validation for an unrelated reason would exercise the wrong branch.
    """
    fields = re.findall(r'name="(value_[a-z0-9_]+)"[^>]*?value="([^"]*)"', page)
    posted = {name: value for name, value in fields if value}
    assert posted, "the son's check-in form rendered no answerable fields"
    return posted


async def test_a_check_in_for_a_hidden_profile_is_recorded_and_answers_with_the_parents_screen(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-276. The write is his and it lands; the 7.7 KB of his screen that used to come back does not.

    D-264 and D-265 keep the write open — a toggle must never stand in front of somebody's data —
    but neither of them considered what the response *body* was. It was his check-in page, youth
    scope and all, on a household that had hidden him.
    """
    posted = _posted_battery((await seeded_client.get("/assess?profile=son")).text)
    before = len(db_session.exec(select(Assessment).where(Assessment.profile_id == PROFILE_SON)).all())
    _set_son_enabled(db_session, False)

    response = await seeded_client.post("/assess?profile=son", data=posted)

    assert response.status_code == SEE_OTHER
    assert response.headers["location"] == "/assess?profile=me"
    assert not response.content

    # The battery really was recorded. Read from the database, because his API reads are 404 now.
    db_session.expire_all()
    after = db_session.exec(select(Assessment).where(Assessment.profile_id == PROFILE_SON)).all()
    assert len(after) > before


async def test_a_rejected_check_in_for_a_hidden_profile_never_redisplays_his_form(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The branch that actually leaked: the redisplay carries his tests in his own vocabulary."""
    _set_son_enabled(db_session, False)
    response = await seeded_client.post("/assess?profile=son", data={"value_push_up_max": "500"})

    assert response.status_code == SEE_OTHER
    assert response.headers["location"] == "/assess?profile=me"
    assert not response.content
    assert "data-profile-kind" not in response.text


async def test_the_htmx_check_in_asks_for_a_navigation_rather_than_swapping_his_result(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """This form posts both ways, and a 303 to HTMX would swap a whole page into the panel."""
    posted = _posted_battery((await seeded_client.get("/assess?profile=son")).text)
    _set_son_enabled(db_session, False)

    response = await seeded_client.post("/assess?profile=son", data=posted, headers={"hx-request": "true"})
    assert response.status_code == 204
    assert response.headers["hx-redirect"] == "/assess?profile=me"
    assert not response.content


async def test_skipping_a_hidden_profiles_check_in_lands_on_the_parents_today(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """No markup ever leaked here — it only redirects — but the ``Location`` named him.

    The skip itself still writes, which is the half that matters: this is the observable proof
    that a hidden profile's HTML writes are not being refused.
    """
    _set_son_enabled(db_session, False)
    before = _planned_status(db_session)

    response = await seeded_client.post("/assess/skip?profile=son", data={})
    assert response.status_code == SEE_OTHER
    assert response.headers["location"] == "/today?profile=me"

    db_session.expire_all()
    assert _planned_status(db_session) != before, "the skip must still land while he is hidden"


def _planned_status(db: DbSession) -> list[tuple[str, str]]:
    rows = db.exec(select(PlannedSession).where(PlannedSession.profile_id == PROFILE_SON)).all()
    return sorted((row.id, row.status) for row in rows)


async def test_the_check_in_is_untouched_while_he_is_shown(seeded_client: httpx.AsyncClient) -> None:
    """The control group: none of the above may change a household that hides nobody."""
    posted = _posted_battery((await seeded_client.get("/assess?profile=son")).text)
    saved = await seeded_client.post("/assess?profile=son", data=posted)
    assert saved.status_code == SEE_OTHER
    assert saved.headers["location"].startswith("/assess?profile=son&saved=1")


# ------------------------------------------------------------------ nothing is deleted


def _son_rows(db: DbSession) -> dict[str, Any]:
    """Everything of the son's that must survive the toggle, as comparable values.

    Ids and stored fields rather than rendered bytes: a page carries ``generated_at`` and a
    relative clock, so anything stricter would fail on the second it happened to run.
    """
    db.expire_all()
    profile = db.get(Profile, PROFILE_SON)
    assert profile is not None
    return {
        "profile": profile.model_dump(mode="json"),
        "programs": sorted(row.id for row in db.exec(select(Program).where(Program.profile_id == PROFILE_SON))),
        "planned": sorted(
            (row.id, row.status, row.rows_json)
            for row in db.exec(select(PlannedSession).where(PlannedSession.profile_id == PROFILE_SON))
        ),
        "sessions": sorted(
            (row.id, row.finished_at, row.felt)
            for row in db.exec(select(SessionRecord).where(SessionRecord.profile_id == PROFILE_SON))
        ),
        "assessments": sorted(
            row.id for row in db.exec(select(Assessment).where(Assessment.profile_id == PROFILE_SON))
        ),
        "challenges": sorted(row.id for row in db.exec(select(Challenge).where(Challenge.profile_id == PROFILE_SON))),
    }


async def test_the_round_trip_leaves_the_son_exactly_where_he_was(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """Seed, train, hide, re-show: his rows are untouched and his screens render as before.

    The snapshot is taken **after** the first ``GET /today``, not before it: that render can queue
    a four-weekly retest (D-226), and comparing against a snapshot from before it would report the
    retest as damage the toggle did.
    """
    # One finished session, so there is real history to lose.
    data = (await seeded_client.get("/api/today?profile=son")).json()["data"]
    session_id = data["session_id"]
    for row in data["rows"]:
        await seeded_client.post(f"/api/sessions/{session_id}/rows/{row['position']}", json={"done": True})
    assert (await seeded_client.post(f"/api/sessions/{session_id}/done", json={"felt": "right"})).status_code == 200

    before_today = (await seeded_client.get("/today?profile=son")).text
    before_history = (await seeded_client.get("/history?profile=son")).text
    before_sessions = (await seeded_client.get("/api/sessions?profile=son")).json()["data"]
    before_rows = _son_rows(db_session)

    _set_son_enabled(db_session, False)
    assert (await seeded_client.get("/today?profile=son")).status_code == SEE_OTHER
    # Hidden, and not one row of his moved while he was.
    assert _son_rows(db_session) == before_rows

    _set_son_enabled(db_session, True)
    assert _son_rows(db_session) == before_rows
    assert (await seeded_client.get("/today?profile=son")).text == before_today
    assert (await seeded_client.get("/history?profile=son")).text == before_history
    assert (await seeded_client.get("/api/sessions?profile=son")).json()["data"] == before_sessions


async def test_hiding_him_does_not_rebuild_anybodys_plan(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """``son_enabled`` is not in ``REBUILD_ON_SETTING`` and must never be added to it.

    A rebuild would hand him a freshly planned block on the way back in, which is the difference
    between hiding a person and starting him over.
    """
    before = _son_rows(db_session)
    response = await seeded_client.put("/api/settings", json={"son_enabled": False})
    assert response.status_code == 200
    assert response.json()["meta"]["rebuilt"] == []
    assert _son_rows(db_session) == before


async def test_the_sons_garmin_setting_is_left_exactly_as_it_was(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """D-260 point 5: ``push_son_to_garmin`` is a different question and this toggle does not answer it."""
    await seeded_client.put("/api/settings", json={"push_son_to_garmin": True})
    await seeded_client.put("/api/settings", json={"son_enabled": False})
    settings = (await seeded_client.get("/api/settings")).json()["data"]
    assert settings["push_son_to_garmin"] is True
    assert settings["son_enabled"] is False


# ------------------------------------------------------------------ the Settings form


async def test_the_toggle_round_trips_through_the_settings_form(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """Off, then on again, through the screen a person actually uses.

    A save that flips this toggle answers with a redirect rather than the re-rendered form: it
    changes the header, which the form's own HTMX swap cannot reach (D-267).
    """
    from cadence.profils import services

    off = await seeded_client.post("/settings", data={"son_enabled": "0", "equipment": ["bodyweight"]})
    assert off.status_code == SEE_OTHER, off.text
    assert off.headers["location"] == "/settings"
    db_session.expire_all()
    assert services.get_settings(db_session).son_enabled is False

    on = await seeded_client.post("/settings", data={"son_enabled": "1", "equipment": ["bodyweight"]})
    assert on.status_code == SEE_OTHER, on.text
    db_session.expire_all()
    assert services.get_settings(db_session).son_enabled is True


async def test_a_save_that_leaves_the_toggle_alone_still_re_renders_the_form(
    seeded_client: httpx.AsyncClient,
) -> None:
    """The redirect is scoped to the change that needs it. Every other save behaves as it did."""
    response = await seeded_client.post("/settings", data={"days_per_week": "3", "equipment": ["bodyweight"]})
    assert response.status_code == 200
    assert "Saved." in response.text


async def test_the_settings_screen_hides_the_controls_that_are_now_meaningless(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """His age, his Garmin push and his VitalForge slug. The overhead anchor stays: D-095 makes it
    a fact about the room, written to both profiles from one control."""
    _set_son_enabled(db_session, False)
    body = (await seeded_client.get("/settings")).text
    for control in ('name="son_age"', 'name="push_son_to_garmin"', 'name="person_son"'):
        assert control not in body, control
    assert 'name="son_enabled"' in body
    assert 'name="has_overhead_anchor"' in body
    assert 'name="person_me"' in body


async def test_a_save_with_the_son_hidden_does_not_clear_his_stored_values(
    seeded_client: httpx.AsyncClient, db_session: DbSession
) -> None:
    """The hidden inputs are not posted, and a field the form did not send is left alone (D-261).

    This is the failure the feature would most plausibly have shipped with: hiding the controls
    and then saving the screen, which without ``_present`` would write his age away as empty.
    """
    await seeded_client.post("/settings", data={"son_age": "11", "person_son": "kid", "equipment": ["bodyweight"]})
    _set_son_enabled(db_session, False)

    # Already off, so this save changes nothing about the header and re-renders in place.
    saved = await seeded_client.post("/settings", data={"son_enabled": "0", "equipment": ["bodyweight"]})
    assert saved.status_code == 200, saved.text

    db_session.expire_all()
    son = db_session.get(Profile, PROFILE_SON)
    assert son.age_years == 11
    assert son.vitalforge_person == "kid"


async def test_setup_always_asks_about_the_son(seeded_client: httpx.AsyncClient, db_session: DbSession) -> None:
    """D-266: the toggle is a Settings control. Setup's job is to record his age either way."""
    _set_son_enabled(db_session, False)
    row = db_session.get(Setting, "setup_complete")
    row.value_json = json.dumps(False)
    db_session.add(row)
    db_session.commit()

    body = (await seeded_client.get("/setup")).text
    assert 'name="son_age"' in body
    assert 'name="son_enabled"' not in body


def test_seeding_writes_both_profiles_whatever_the_setting_says(db_session: DbSession, seeded) -> None:
    """D-260 point 7: the setting hides, it does not change what ``make seed`` builds."""
    assert db_session.get(Profile, PROFILE_ME) is not None
    assert db_session.get(Profile, PROFILE_SON) is not None
    assert db_session.get(Setting, "son_enabled").value_json == json.dumps(True)


def test_the_settings_object_round_trips_the_new_key() -> None:
    rows = DEFAULT_SETTINGS.with_changes(son_enabled=False).as_rows()
    assert rows["son_enabled"] == json.dumps(False)
    assert ProgramSettings.model_validate({"son_enabled": False}).son_enabled is False
