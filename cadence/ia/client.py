"""The OmniRoute client: one OpenAI-compatible chat completion, never streamed.

Three things this module will not do, each because doing it once would be a leak that outlives
the request. It never logs the prompt, the reply body or the key; it never streams (the brief's
gateway gotcha); and it never puts a gateway error message into a user-facing string, because a
401 from a proxy has been known to echo the credential it rejected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import httpx

from cadence.config import Settings

logger = logging.getLogger(__name__)

# Connect fast, read slow: a gateway that is down should fail in seconds, and a model that is
# thinking is allowed the full minute. Never ``requests`` - a blocking call here would hold the
# event loop for that minute and stop Today from rendering (PRP-08 risk 12).
TIMEOUT = httpx.Timeout(60.0, connect=5.0)
TEMPERATURE = 0.4
COMPLETIONS_PATH = "chat/completions"

# One deadline for the whole exchange, and a ceiling on what a reply may weigh. A workout is
# under two kilobytes; half a megabyte is generous and still bounded.
DEADLINE_S = 60.0
MAX_RESPONSE_BYTES = 512 * 1024

MOCK_MODEL = "mock"

# The one seam a test uses to stand in for the gateway: ``None`` in production, an
# ``httpx.MockTransport`` under test. It lives here rather than on a router so the JSON API and
# the Settings screen are mocked by the same switch and cannot diverge.
TRANSPORT: httpx.AsyncBaseTransport | None = None

# The canned reply behind ``CADENCE_OMNIROUTE_MODE=mock``. It exists so the deploy smoke test
# (PRP-09) can drive Generate on a box that will never hold a key. It goes through every gate a
# real reply does - the mock switch bypasses the network, never the checks.
#
# ``both``, not ``adult``: one canned document answers whichever profile the smoke test names,
# and the son is the likelier target. Declared ``adult`` it failed ``profile_kind_mismatch`` for
# every youth profile, retried, failed again and answered "could not produce a workout" - a
# validator refusal wearing the costume of a gateway problem, on the one configuration that has
# no gateway to blame (D-189). Every row is bodyweight and within the strictest youth band, so
# the document earns the wider claim rather than merely asserting it.
MOCK_REPLY = """id: mock-posture-focus
name: Posture focus
day_type: upper_a
target_profile_kind: both
estimated_minutes: 20
rows:
  - exercise_id: wall-angel
    sets: 2
    reps: 10
    load_unit: bodyweight
    rest_s: 45
  - exercise_id: dead-bug
    sets: 2
    reps: 8
    load_unit: bodyweight
    rest_s: 45
  - exercise_id: glute-bridge
    sets: 2
    reps: 12
    load_unit: bodyweight
    rest_s: 45
"""


class OmniRouteError(RuntimeError):
    """The gateway could not be reached or did not answer with a completion.

    Its message is written here and never taken from the response body, so nothing the gateway
    says can reach a screen or a log line.
    """


@dataclass(frozen=True, slots=True)
class Completion:
    """What came back: the text, the model that produced it, and how long it took."""

    text: str
    model: str
    latency_ms: int


def request_body(prompt: str, model: str) -> dict[str, object]:
    """The completion request. ``stream`` is explicitly false, never merely absent."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": TEMPERATURE,
        "stream": False,
    }


async def complete(
    prompt: str,
    *,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Completion:
    """One completion against OmniRoute, or ``OmniRouteError``.

    ``transport`` is the test seam: ``httpx.MockTransport`` stands in for the gateway, so no test
    in this repo ever needs a key or a network.
    """
    if settings.omniroute_mocked:
        logger.info("omniroute mock mode: returning the canned workout without a network call")
        return Completion(text=MOCK_REPLY, model=MOCK_MODEL, latency_ms=0)

    key = settings.omniroute_key.get_secret_value()
    if not key:
        # Callers check ``omniroute_configured`` first; this is the second lock on the same door.
        raise OmniRouteError("generation is not configured on this install")

    model = settings.omniroute_model_generate
    url = f"{str(settings.omniroute_url).rstrip('/')}/{COMPLETIONS_PATH}"
    started = time.monotonic()
    try:
        payload = await _stream_completion(url, key, model, prompt, transport)
    except TimeoutError as exc:
        raise OmniRouteError("the model did not answer within a minute") from exc
    except httpx.TimeoutException as exc:
        raise OmniRouteError("the model did not answer within a minute") from exc
    except httpx.HTTPError as exc:
        raise OmniRouteError("the model gateway could not be reached") from exc
    latency_ms = int((time.monotonic() - started) * 1000)

    text = _first_message(payload)
    logger.info("omniroute completion: model=%s latency_ms=%d chars=%d", model, latency_ms, len(text))
    return Completion(text=text, model=model, latency_ms=latency_ms)


async def _stream_completion(
    url: str,
    key: str,
    model: str,
    prompt: str,
    transport: httpx.AsyncBaseTransport | None,
) -> bytes:
    """The reply body, read under a byte cap and one deadline for the whole exchange.

    ``httpx.Timeout`` bounds each *read*, not the exchange: a gateway dripping one byte every four
    seconds resets the read timer for ever and holds a worker open with it. ``asyncio.timeout``
    bounds the whole thing regardless of drip rate, and the running total bounds what an
    unbounded or hostile gateway can make this process hold in memory (Codex finding 8).
    """
    chunks: list[bytes] = []
    total = 0
    async with asyncio.timeout(DEADLINE_S):
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=transport or TRANSPORT) as http:
            async with http.stream(
                "POST",
                url,
                json=request_body(prompt, model),
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            ) as response:
                if response.status_code >= 400:
                    # The status and nothing else. A gateway's own error body is not ours to
                    # repeat, and it is not read at all.
                    logger.warning("omniroute answered %s for model %s", response.status_code, model)
                    raise OmniRouteError(f"the model gateway refused the request ({response.status_code})")
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise OmniRouteError(
                            f"the model gateway sent more than {MAX_RESPONSE_BYTES // 1024} KB; the reply was dropped"
                        )
                    chunks.append(chunk)
    return b"".join(chunks)


def _first_message(raw: bytes) -> str:
    """The one message an OpenAI-compatible reply carries, or a refusal naming only the shape."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise OmniRouteError("the model gateway answered with something that is not JSON") from exc
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OmniRouteError("the model gateway answered without a completion") from exc
    if not isinstance(content, str) or not content.strip():
        raise OmniRouteError("the model returned an empty answer")
    return content


__all__ = [
    "DEADLINE_S",
    "MAX_RESPONSE_BYTES",
    "MOCK_MODEL",
    "MOCK_REPLY",
    "TEMPERATURE",
    "TIMEOUT",
    "Completion",
    "OmniRouteError",
    "complete",
]
