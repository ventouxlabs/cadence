"""The page-weight gate: `/today` and everything it pulls, against the D-025 budget.

`docs/HANDOFF.md` and `README.md` are asserted in `tests/test_handoff.py`, which owns every
claim about those two documents; this file is only about bytes on the wire.

The checker runs against an in-process app over an ASGI transport rather than a live server: the
middleware is the thing being measured and it runs identically either way, and `make test` blocks
real sockets on purpose (D-133). ``make perf`` points the same functions at a real URL.
"""

from __future__ import annotations

import base64
import gzip
import os
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.perf import BUDGETS, check, measure  # noqa: E402 - the path has to be set first

BASE = "http://testserver"


def _factory(app: FastAPI):
    """A client for the checker that speaks to the app in process.

    ``TestClient`` rather than a bare ``ASGITransport``: the transport is async-only, and the
    checker is synchronous because ``make perf`` runs it against a real deployment. ``TestClient``
    *is* an ``httpx.Client``, so the same code path serves both.
    """

    def build() -> httpx.Client:
        return TestClient(app, base_url=BASE, follow_redirects=True)

    return build


@pytest.fixture
def measured(seeded: FastAPI):
    def run(profile: str):
        return measure(BASE, profile, _factory(seeded))

    return run


# ------------------------------------------------------------------------------ the budget


@pytest.mark.parametrize("profile", ["me", "son"])
def test_perf_budget_today(measured, profile: str) -> None:
    """Both profiles fit. The son's bigger type and icons are the interesting half."""
    assets, breaches = measured(profile)
    assert not breaches, [str(item) for item in breaches]
    total = sum(asset.transfer for asset in assets)
    assert 0 < total <= BUDGETS["total"], f"{profile} transferred {total} B"
    for kind, ceiling in BUDGETS.items():
        if kind == "total":
            continue
        assert sum(a.transfer for a in assets if a.kind == kind) <= ceiling


def test_the_son_pays_for_his_icons_once(measured) -> None:
    """The sprite is one <symbol> block reused by <use>, so the son's page is not double the parent's."""
    parent = sum(asset.transfer for asset in measured("me")[0])
    son = sum(asset.transfer for asset in measured("son")[0])
    assert son < parent * 1.5, f"the son's page is {son} B against the parent's {parent} B"


def test_perf_fails_without_gzip(settings) -> None:
    """The gate fires. Built without the middleware, the same checker returns breaches."""
    from cadence.main import create_app

    app = create_app(settings)
    app.user_middleware = [entry for entry in app.user_middleware if "GZip" not in str(entry)]
    app.middleware_stack = app.build_middleware_stack()

    breaches = check(BASE, _factory(app))
    assert breaches, "a page served uncompressed passed the budget check"
    assert any("gzip" in str(item) for item in breaches), [str(item) for item in breaches]


# ------------------------------------------------------- the gate fires on an oversized page
#
# `test_perf_fails_without_gzip` proves the compression half. These prove the other half: the
# per-asset ceilings and the 60 KB total at `scripts/perf.py:174-183`, which no test reached.
# A canned transport rather than a real app, because the point is to serve a page that breaches
# the budget and no real Cadence page does — and `make test` blocks real sockets (D-133).


#: How far under its target `_bulk` can land: one trimming step.
BULK_TOLERANCE = 0.02


def _bulk(target: int) -> bytes:
    """ASCII that gzips to just under `target`, never over it.

    The checker measures the *compressed* length (D-237), so that is the number to aim at, and
    base64 of random bytes is the one payload whose compressed size is predictable — a 6-bit
    alphabet stored in 8-bit bytes, so gzip hands back roughly the entropy that went in. Trimmed
    down to the target rather than grown up to it, so a test can ask for a payload that has to
    stay under a ceiling as easily as one that has to cross it.
    """
    payload = base64.b64encode(os.urandom(target + 4096))
    while len(gzip.compress(payload)) > target:
        payload = payload[: int(len(payload) * (1 - BULK_TOLERANCE))]
    return payload


def _canned(routes: dict[str, bytes]):
    """A client serving a fixed path -> body map, gzipped the way the real middleware serves it."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, content=gzip.compress(body), headers={"content-encoding": "gzip"})

    def build() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE, follow_redirects=True)

    return build


def _document(*references: str) -> bytes:
    links = "".join(
        f'<script src="{ref}"></script>' if ref.endswith(".js") else f'<link href="{ref}">' for ref in references
    )
    return f"<!doctype html><html><head>{links}</head><body>ok</body></html>".encode()


@pytest.mark.parametrize(
    ("kind", "asset"),
    [("js", "/static/app.js"), ("css", "/static/style.css"), ("other", "/static/icon.svg")],
)
def test_an_asset_over_its_ceiling_is_a_breach(kind: str, asset: str) -> None:
    """One oversized subresource fails the build, and the message names the category."""
    routes = {"/today": _document(asset), asset: _bulk(BUDGETS[kind] + 3072)}

    breaches = measure(BASE, "me", _canned(routes))[1]
    ceiling = [item for item in breaches if f"{kind} is " in str(item)]
    assert ceiling, [str(item) for item in breaches]
    assert f"over its {BUDGETS[kind]} B ceiling" in str(ceiling[0]), str(ceiling[0])


def test_an_oversized_document_breaches_the_html_ceiling() -> None:
    routes = {"/today": _document() + _bulk(BUDGETS["html"] + 3072)}
    breaches = measure(BASE, "me", _canned(routes))[1]
    assert [item for item in breaches if "html is " in str(item)], [str(item) for item in breaches]


def test_a_page_under_every_ceiling_can_still_breach_the_total() -> None:
    """The 60 KB aggregate is the gate, not the sum of the ceilings (D-025).

    Three assets each comfortably inside its own limit add up to more than the total, which is
    the case the per-asset lines alone would wave through.
    """
    routes = {
        # Each asset sits on its own ceiling; the three ceilings sum to 65 KB, over the 60 KB gate.
        "/today": _document("/static/app.js", "/static/style.css") + _bulk(BUDGETS["html"] - 1024),
        "/static/app.js": _bulk(BUDGETS["js"]),
        "/static/style.css": _bulk(BUDGETS["css"]),
    }

    assets, breaches = measure(BASE, "me", _canned(routes))
    for kind in ("html", "js", "css"):
        used = sum(asset.transfer for asset in assets if asset.kind == kind)
        assert used <= BUDGETS[kind], f"{kind} broke its own ceiling, so this proves nothing"
    assert sum(asset.transfer for asset in assets) > BUDGETS["total"]
    assert [item for item in breaches if "over the" in str(item) and "gate" in str(item)], [
        str(item) for item in breaches
    ]


def test_the_canned_transport_passes_a_small_page() -> None:
    """The harness itself is not the thing failing: a tiny page through it reports nothing."""
    routes = {"/today": _document("/static/app.js"), "/static/app.js": b"// hi"}
    assert measure(BASE, "me", _canned(routes))[1] == []


def test_an_external_host_is_a_breach() -> None:
    """A CDN reference fails, and `www.w3.org` still does not (D-239)."""
    routes = {"/today": b'<!doctype html><html><body><a href="https://unpkg.com/htmx">x</a></body></html>'}
    assert [item for item in measure(BASE, "me", _canned(routes))[1] if "unpkg.com" in str(item)]

    allowed = {"/today": b'<!doctype html><svg xmlns="http://www.w3.org/2000/svg"></svg>'}
    assert measure(BASE, "me", _canned(allowed))[1] == []


def test_a_webfont_is_a_breach() -> None:
    routes = {"/today": b"<!doctype html><style>@font-face{font-family:x}</style>"}
    assert [item for item in measure(BASE, "me", _canned(routes))[1] if "webfont" in str(item)]


def test_the_checker_is_not_vacuous(measured) -> None:
    """It really fetched the document and its subresources, rather than passing on an empty list."""
    assets = measured("me")[0]
    kinds = {asset.kind for asset in assets}
    assert {"html", "js", "css"} <= kinds, kinds
    assert all(asset.transfer > 0 for asset in assets)


@pytest.mark.parametrize("profile", ["me", "son"])
def test_no_external_hosts(measured, profile: str) -> None:
    assets, breaches = measured(profile)
    assert not [item for item in breaches if "external host" in str(item)]
    assert len(assets) > 1


@pytest.mark.parametrize("profile", ["me", "son"])
def test_no_webfont(measured, profile: str) -> None:
    """No `@font-face`, no `preconnect`: the system stack only, so nothing blocks first paint."""
    assert not [item for item in measured(profile)[1] if "webfont" in str(item)]


def test_every_inline_svg_declares_its_box() -> None:
    """An SVG without width and height has no intrinsic size and shifts the row as it paints."""
    row = (REPO / "cadence/web/templates/partials/row.html").read_text()
    for tag in [line for line in row.splitlines() if "<svg" in line]:
        assert 'width="32"' in tag and 'height="32"' in tag, tag
