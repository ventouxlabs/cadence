"""PRP-06 acceptance tests 1-8: the HTTP client.

Every one of these is about something that fails silently in production. The bearer header, the
two base URLs, the five-second timeout and the sanitised error body are all invisible until the
day they are wrong, and the token in a log line is invisible even then.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from cadence.config import Settings
from cadence.vitalforge import mock
from cadence.vitalforge.client import TIMEOUT_S, VitalForgeClient
from cadence.vitalforge.errors import (
    VitalForgeHTTPError,
    VitalForgeNotConfigured,
    VitalForgeUnavailable,
    sanitise,
)
from tests.conftest import VF_TOKEN

WEIGHT = "http://weight.test"
DASH = "http://dash.test"
SLUG = "jd"

ACTIVITY_BODY = {"session_id": "s-1", "start": "2026-09-06T08:00:00+00:00", "duration_min": 42, "exercises": []}


@pytest.fixture
def client(live_settings: Settings) -> VitalForgeClient:
    return VitalForgeClient(live_settings)


async def test_bearer_header_sent(client: VitalForgeClient) -> None:
    """Test 1. Contract section 1.3: a bearer token, never the session cookie."""
    route = respx.get(f"{DASH}/p/{SLUG}/api/readiness").mock(return_value=httpx.Response(200, json={"score": 72}))

    await client.get_readiness(SLUG)

    assert route.calls.last.request.headers["Authorization"] == f"Bearer {VF_TOKEN}"
    assert "cookie" not in {name.lower() for name in route.calls.last.request.headers}


async def test_token_never_logged(client: VitalForgeClient, caplog: pytest.LogCaptureFixture) -> None:
    """Test 2. A success, a 500 and a timeout: none of the three may leak the token.

    The timeout is the dangerous one. ``httpx``'s own exceptions carry the request, the request
    carries the header, and one ``logger.exception`` upstream would write it to disk.
    """
    caplog.set_level(logging.DEBUG)
    respx.get(f"{DASH}/p/{SLUG}/api/readiness").mock(return_value=httpx.Response(200, json={"score": 72}))
    await client.get_readiness(SLUG)

    respx.get(f"{DASH}/p/{SLUG}/api/metrics/weight").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(VitalForgeHTTPError):
        await client.get_metric(SLUG, "weight")

    respx.get(f"{DASH}/p/{SLUG}/api/metrics/body_fat").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(VitalForgeUnavailable) as timed_out:
        await client.get_metric(SLUG, "body_fat")

    assert VF_TOKEN not in caplog.text
    assert "Bearer" not in caplog.text
    assert VF_TOKEN not in str(timed_out.value)


async def test_uses_weight_url_for_activity_and_dashboard_url_for_metrics(client: VitalForgeClient) -> None:
    """Test 3. The split-service trap: an activity sent to the dashboard is a 404 that looks
    exactly like "PRP-05 is not deployed yet", and would be chased for hours."""
    activity = respx.post(f"{WEIGHT}/p/{SLUG}/api/activity").mock(return_value=httpx.Response(202, json={"id": 7}))
    metric = respx.get(f"{DASH}/p/{SLUG}/api/metrics/weight").mock(return_value=httpx.Response(200, json={"data": []}))
    recent = respx.get(f"{WEIGHT}/p/{SLUG}/api/weight/recent").mock(return_value=httpx.Response(200, json=[]))

    await client.post_activity(SLUG, ACTIVITY_BODY)
    await client.get_metric(SLUG, "weight")
    await client.get_recent_weight(SLUG)

    assert activity.calls.last.request.url.host == "weight.test"
    assert recent.calls.last.request.url.host == "weight.test"
    assert metric.calls.last.request.url.host == "dash.test"


def test_timeout_is_five_seconds(client: VitalForgeClient) -> None:
    """Test 4. Done attempts inline, so this is also the longest anyone waits for the summary."""
    assert client.timeout.read == TIMEOUT_S == 5.0
    assert client.timeout.connect == 5.0


async def test_transport_error_raises_vitalforge_unavailable(client: VitalForgeClient) -> None:
    """Test 5. No ``httpx`` exception escapes the package, whatever the caller does with it."""
    respx.get(f"{DASH}/p/{SLUG}/api/readiness").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(VitalForgeUnavailable) as raised:
        await client.get_readiness(SLUG)

    assert not isinstance(raised.value, httpx.HTTPError)
    assert "ConnectError" in str(raised.value)


async def test_blank_slug_never_issues_request(live_settings: Settings) -> None:
    """Test 6. ``/p//api/readiness`` is a 404 that reads like a missing deployment."""
    client = VitalForgeClient(live_settings)

    with pytest.raises(VitalForgeNotConfigured):
        await client.get_readiness("")
    with pytest.raises(VitalForgeNotConfigured):
        await client.post_activity("  ", ACTIVITY_BODY)

    assert respx.calls.call_count == 0


async def test_blank_token_never_issues_request(db_path, clean_env: None) -> None:
    """Test 6, other half. No token is a configuration gap, not a request worth making."""
    settings = Settings(
        CADENCE_DB_PATH=db_path,
        CADENCE_VITALFORGE_MODE="live",
        VITALFORGE_DASHBOARD_URL=DASH,
        VITALFORGE_TOKEN="",
        _env_file=None,
    )

    with pytest.raises(VitalForgeNotConfigured):
        await VitalForgeClient(settings).get_readiness(SLUG)

    assert respx.calls.call_count == 0


async def test_error_body_is_truncated_and_sanitised(client: VitalForgeClient) -> None:
    """Test 7. A 5 000-character error page echoing the request headers back at us."""
    body = ("x" * 2000) + f"Authorization: Bearer {VF_TOKEN}" + ("y" * 3000)
    respx.get(f"{DASH}/p/{SLUG}/api/metrics/weight").mock(return_value=httpx.Response(500, text=body))

    with pytest.raises(VitalForgeHTTPError) as raised:
        await client.get_metric(SLUG, "weight")

    assert len(raised.value.body_excerpt) <= 500
    assert VF_TOKEN not in raised.value.body_excerpt
    assert VF_TOKEN not in str(raised.value)


def test_sanitise_strips_every_bearer_shape() -> None:
    """The regex, directly: ``last_error`` is the column an operator reads."""
    assert VF_TOKEN not in sanitise(f"bearer {VF_TOKEN} and Bearer {VF_TOKEN}")
    assert len(sanitise("the server said no. " * 90)) == 500


async def test_mock_mode_makes_no_request(settings: Settings) -> None:
    """Test 8. PRP-09's smoke script runs the whole app this way, with no token at all."""
    client = VitalForgeClient(settings)

    readiness = await client.get_readiness("")
    metric = await client.get_metric("", "muscle_mass")
    result = await client.post_activity("", ACTIVITY_BODY)

    assert respx.calls.call_count == 0
    assert readiness["score"] == mock.READINESS_SCORE
    assert metric["data"][-1]["value"] == float(mock.MUSCLE_MASS_GRAMS)
    assert result.status_code == 202
    assert mock.POSTED[-1]["payload"] == ACTIVITY_BODY


async def test_dedup_and_remote_ref_are_read_off_the_body(client: VitalForgeClient) -> None:
    """A 200 with ``deduplicated`` is VitalForge saying it already had this session."""
    respx.post(f"{WEIGHT}/p/{SLUG}/api/activity").mock(
        return_value=httpx.Response(200, json={"id": 7, "deduplicated": True, "garmin_status": "synced"})
    )

    result = await client.post_activity(SLUG, ACTIVITY_BODY)

    assert result.stored and result.deduplicated
    assert result.remote_id == "7"
    assert result.garmin_status == "synced"


async def test_a_4xx_on_activity_is_a_result_not_an_exception(client: VitalForgeClient) -> None:
    """The sync queue decides on the status code, so ``post_activity`` hands it back rather
    than raising: a 409 and a 500 need opposite treatment."""
    respx.post(f"{WEIGHT}/p/{SLUG}/api/activity").mock(
        return_value=httpx.Response(409, json={"detail": "no Garmin account of their own"})
    )

    result = await client.post_activity(SLUG, ACTIVITY_BODY)

    assert result.status_code == 409 and not result.stored
    assert "no Garmin account" in (result.error or "")


async def test_the_token_is_redacted_even_when_nothing_says_bearer(client: VitalForgeClient, caplog) -> None:
    """Codex A. VitalForge's own error text can name the value it rejected.

    ``invalid token: <the token>`` has no "Bearer" in it, so the old rule let it through into the
    exception, the log and ``sync_job.last_error`` — three places a token is not supposed to be.
    """
    import logging

    caplog.set_level(logging.DEBUG)
    respx.get(f"{DASH}/p/{SLUG}/api/readiness").mock(
        return_value=httpx.Response(401, text=f"invalid token: {VF_TOKEN} (rotate it)")
    )

    with pytest.raises(VitalForgeHTTPError) as raised:
        await client.get_readiness(SLUG)

    assert VF_TOKEN not in raised.value.body_excerpt
    assert VF_TOKEN not in str(raised.value)
    assert VF_TOKEN not in caplog.text
    assert "rotate it" in raised.value.body_excerpt, "redaction ate the part that helps"


async def test_a_token_shaped_string_is_redacted_even_unregistered(client: VitalForgeClient) -> None:
    """A rotated token, a second deployment's token, anything of that shape. Caught by shape."""
    other = "8pQ3xLm2ZkR9tYw7NvB4cD6fH1jS5aE0gU"
    respx.get(f"{DASH}/p/{SLUG}/api/readiness").mock(return_value=httpx.Response(500, text=f"bad key {other}"))

    with pytest.raises(VitalForgeHTTPError) as raised:
        await client.get_readiness(SLUG)

    assert other not in raised.value.body_excerpt


async def test_a_session_id_survives_redaction(client: VitalForgeClient) -> None:
    """A uuid4 is the same alphabet and the same length, and is the one thing in ``last_error``
    that tells an operator which session the message is about."""
    session_id = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
    respx.get(f"{DASH}/p/{SLUG}/api/readiness").mock(
        return_value=httpx.Response(500, text=f"session {session_id} exploded")
    )

    with pytest.raises(VitalForgeHTTPError) as raised:
        await client.get_readiness(SLUG)

    assert session_id in raised.value.body_excerpt


def test_production_refuses_a_mocked_integration(db_path, clean_env) -> None:
    """Codex B. ``mode=mock`` answers every write 202 without opening a socket.

    In production that is every session reported "synced ✓" while VitalForge hears nothing and
    Garmin gets nothing, and no downstream check can tell: from Cadence's side it looks exactly
    like success. Startup is the last place it can be caught.
    """
    with pytest.raises(ValueError, match="refuses mock integrations"):
        Settings(CADENCE_DB_PATH=db_path, CADENCE_ENV="prod", CADENCE_VITALFORGE_MODE="mock", _env_file=None)

    live = Settings(CADENCE_DB_PATH=db_path, CADENCE_ENV="prod", CADENCE_VITALFORGE_MODE="live", _env_file=None)
    assert live.vitalforge_mode == "live"


def test_mock_mode_is_fine_outside_production(db_path, clean_env) -> None:
    for env in ("dev", "test"):
        assert Settings(CADENCE_DB_PATH=db_path, CADENCE_ENV=env, CADENCE_VITALFORGE_MODE="mock", _env_file=None)


def test_the_mock_activity_id_is_derived_from_the_session() -> None:
    """Stable across runs and unique per session.

    It used to be ``len(POSTED)``, so the same session got a different ``remote_ref`` depending
    on how many payloads the process had already recorded - which made it depend on test order,
    and let two different sessions share one id.
    """
    first = mock.activity("session-a")["id"]

    assert mock.activity("session-a")["id"] == first
    assert mock.activity("session-b")["id"] != first
    assert first.startswith("mock-")


async def test_the_mock_recorder_is_empty_at_the_start_of_a_test(settings: Settings) -> None:
    """The autouse fixture, asserted: shared state that nobody clears is shared state that
    makes the next test's assertion depend on this one."""
    assert mock.POSTED == []

    await VitalForgeClient(settings).post_activity("jd", ACTIVITY_BODY)

    assert len(mock.POSTED) == 1
