"""The same-origin guard: that it covers every route that writes, and that it cannot be talked round.

The coverage tests walk the route table rather than listing routes, so a router added next year is
checked without anyone remembering to add it here (the gap D-186 flagged and PRP-08 half-closed).
"""

from __future__ import annotations

import re

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from starlette.requests import Request

from cadence.web.guards import UNSAFE_METHODS, RequestRefused, require_same_origin

# A state-changing route that is deliberately NOT behind the guard goes here, with the reason.
# Empty on purpose: every write route in Cadence is reachable from a browser tab.
INTENTIONALLY_UNGUARDED: frozenset[tuple[str, str]] = frozenset()


def _depends_on_guard(dependant) -> bool:
    return any(sub.call is require_same_origin or _depends_on_guard(sub) for sub in dependant.dependencies)


def unguarded_routes(application: FastAPI) -> list[tuple[str, str]]:
    """Every (method, path) that can write and does not resolve ``require_same_origin``."""
    found: list[tuple[str, str]] = []
    for route in application.routes:
        if not isinstance(route, APIRoute) or _depends_on_guard(route.dependant):
            continue
        found.extend(
            (method, route.path)
            for method in sorted(route.methods & UNSAFE_METHODS)
            if (method, route.path) not in INTENTIONALLY_UNGUARDED
        )
    return found


def _unsafe_routes(application: FastAPI) -> list[tuple[str, str]]:
    return sorted(
        (method, route.path)
        for route in application.routes
        if isinstance(route, APIRoute)
        for method in route.methods & UNSAFE_METHODS
    )


# ------------------------------------------------------------------ coverage of the route table


def test_the_walker_finds_an_unguarded_route() -> None:
    """The walker is the test; prove it can fail."""
    bare = FastAPI()
    bare.post("/write")(lambda: None)
    router = APIRouter(dependencies=[Depends(require_same_origin)])
    router.put("/guarded")(lambda: None)
    bare.include_router(router)
    assert unguarded_routes(bare) == [("POST", "/write")]


def test_every_route_that_writes_is_behind_the_guard(app: FastAPI) -> None:
    assert unguarded_routes(app) == []


def test_no_get_route_writes(app: FastAPI) -> None:
    """The guard only looks at unsafe methods, so a GET that writes would slip past it.

    Heuristic, not proof: the GET handlers' source must not commit or add to a session.
    """
    import inspect

    offenders = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.methods <= {"GET", "HEAD"}:
            source = inspect.getsource(route.endpoint)
            if re.search(r"\.commit\(|\.add\(|\.delete\(", source):
                offenders.append(route.path)
    assert offenders == []


def test_the_walker_sees_the_routes_the_audit_named(app: FastAPI) -> None:
    routes = _unsafe_routes(app)
    assert ("POST", "/settings/profiles/{profile_id}/kind") in routes
    assert ("PUT", "/api/profiles/{profile_id}/kind") in routes
    assert len(routes) >= 20


@pytest.mark.parametrize(
    ("method", "path"),
    [pytest.param(m, p, id=f"{m} {p}") for m, p in _unsafe_routes(__import__("cadence.main", fromlist=["app"]).app)],
)
async def test_a_forged_origin_is_refused_on_every_write_route(
    client: httpx.AsyncClient, method: str, path: str
) -> None:
    url = re.sub(r"\{[^}]+\}", "1", path)
    response = await client.request(method, url, headers={"origin": "http://evil.example"})
    assert response.status_code == 422, (method, path, response.text)
    assert "another site" in response.text


# ------------------------------------------------------------------ the guard itself


def _request(method: str = "POST", **headers: str | list[str]) -> Request:
    raw: list[tuple[bytes, bytes]] = []
    for name, value in headers.items():
        for item in value if isinstance(value, list) else [value]:
            raw.append((name.replace("_", "-").encode(), item.encode()))
    scope = {"type": "http", "method": method, "path": "/x", "headers": raw, "query_string": b""}
    return Request(scope)


def _refused(request: Request) -> bool:
    try:
        require_same_origin(request)
    except RequestRefused:
        return True
    return False


HOST = "cadence.tail1234.ts.net:8090"


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example",
        "http://evil.example:8090",
        "https://cadence.tail1234.ts.net:8090.evil.example",
        "http://evil.cadence.tail1234.ts.net:8090",
        "http://xcadence.tail1234.ts.net:8090",
        "http://cadence.tail1234.ts.net",  # same host, other port
        "http://cadence.tail1234.ts.net:8091",
        "http://cadence.tail1234.ts.net:8090@evil.example",
        "http://evil.example@cadence.tail1234.ts.net:8090",  # userinfo is never a real Origin
        "http://cadence.tail1234.ts.net:8090\\@evil.example",
        "http://evil.example\\.cadence.tail1234.ts.net:8090",
        "null",
        "NULL",
        "",
        "   ",
        "cadence.tail1234.ts.net:8090",  # no scheme: not an origin
        "ftp://cadence.tail1234.ts.net:8090",
        "http://",
        "http://[::1",
        "http://cadence.tail1234.ts.net:notaport",
    ],
)
def test_a_hostile_or_malformed_origin_is_refused(origin: str) -> None:
    assert _refused(_request(host=HOST, origin=origin))


@pytest.mark.parametrize("referer", ["null", "http://evil.example/x", "evil.example", "javascript:alert(1)"])
def test_a_hostile_or_malformed_referer_is_refused(referer: str) -> None:
    assert _refused(_request(host=HOST, referer=referer))


def test_the_second_origin_header_is_not_ignored() -> None:
    assert _refused(_request(host=HOST, origin=["http://cadence.tail1234.ts.net:8090", "http://evil.example"]))


def test_an_origin_with_no_host_header_is_refused() -> None:
    assert _refused(_request(origin="http://evil.example"))


def test_a_malformed_host_with_an_origin_is_refused() -> None:
    assert _refused(_request(host="[::1", origin="http://evil.example"))


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        (HOST, "http://cadence.tail1234.ts.net:8090"),
        (HOST, "https://cadence.tail1234.ts.net:8090"),
        (HOST, "HTTP://CADENCE.Tail1234.TS.NET:8090"),
        (HOST, "http://cadence.tail1234.ts.net.:8090"),  # trailing dot is the same host
        ("cadence.ts.net.", "https://cadence.ts.net"),
        ("cadence.ts.net", "https://cadence.ts.net:443"),  # default ports are implicit
        ("cadence.ts.net:80", "http://cadence.ts.net"),
        ("[::1]:8090", "http://[::1]:8090"),
        ("localhost:8090", "http://localhost:8090"),
    ],
)
def test_the_same_origin_is_allowed(host: str, origin: str) -> None:
    assert not _refused(_request(host=host, origin=origin))
    assert not _refused(_request(host=host, referer=f"{origin}/settings?x=1#y"))


def test_no_origin_and_no_referer_is_allowed() -> None:
    assert not _refused(_request(host=HOST))
    assert not _refused(_request())


def test_a_safe_method_is_never_refused() -> None:
    assert not _refused(_request("GET", host=HOST, origin="null"))
