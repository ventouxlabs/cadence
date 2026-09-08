#!/usr/bin/env -S uv run python
"""The page-weight gate: `/today` and everything it pulls, in gzip transfer bytes.

D-025 fixes the budgets and architecture section 5 makes the 60 KB aggregate the number that
fails the build. The per-asset ceilings sum above it on purpose — they are limits no single file
may cross, the total is the gate.

Transfer bytes, not file bytes. Vendored HTMX is roughly 48 KB on disk and about a third of that
over the wire, so a checker that measured `len(response.content)` would fail a page that is
comfortably inside budget. `Content-Length` on a gzipped response *is* the transfer size, and a
response that arrives without `Content-Encoding: gzip` is itself a breach: the middleware is the
only thing standing between this app and a 150 KB Today on a cheap phone.

Importable as well as runnable. `check(base_url)` returns the breaches, so a test can point it at
an app built without `GZipMiddleware` and assert that it complains, without a subprocess dance.

    make perf                       # against http://localhost:8090
    make perf BASE=https://cadence.grepon.cc
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

#: D-025, in bytes. ``total`` is the gate; the rest are per-category ceilings.
BUDGETS: dict[str, int] = {
    "html": 30 * 1024,
    "js": 25 * 1024,
    "css": 10 * 1024,
    "other": 5 * 1024,
    "total": 60 * 1024,
}

PROFILES = ("me", "son")
TIMEOUT_S = 15.0

# Subresources the document pulls. Deliberately literal: this is a gate, and a real HTML parser
# would let a resource in through a form the regex does not know about without anyone noticing.
_HREF = re.compile(r"""<link\b[^>]*\bhref=["']([^"']+)["']""", re.I)
_SRC = re.compile(r"""<script\b[^>]*\bsrc=["']([^"']+)["']""", re.I)
_IMG = re.compile(r"""<img\b[^>]*\bsrc=["']([^"']+)["']""", re.I)

# Anything pointing off this origin. `//cdn` catches a protocol-relative URL, which has no scheme
# and would slip past a plain `https?://` search.
_EXTERNAL = re.compile(r"""["'(]\s*(?:https?:)?//(?!/)([a-z0-9.-]+)""", re.I)
# XML namespace URIs, which name a specification and are never fetched. `xmlns="http://www.w3.org/
# 2000/svg"` is required markup on a standalone SVG file, so treating it as an external host would
# make the gate un-passable rather than strict.
_NAMESPACE_HOSTS = frozenset({"www.w3.org"})
_WEBFONT = re.compile(r"@font-face|rel=[\"']?preconnect|rel=[\"']?preload[^>]*as=[\"']?font", re.I)


@dataclass(frozen=True, slots=True)
class Asset:
    url: str
    kind: str
    transfer: int
    raw: int
    gzipped: bool


@dataclass(frozen=True, slots=True)
class Breach:
    """One thing wrong with the page. Any breach at all fails the build."""

    where: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where}: {self.detail}"


def _kind(url: str) -> str:
    path = urlsplit(url).path.lower()
    if path.endswith(".js"):
        return "js"
    if path.endswith(".css"):
        return "css"
    if path.endswith((".json", ".svg", ".png", ".ico", ".webmanifest")):
        return "other"
    return "html"


def _transfer_bytes(response: httpx.Response) -> int:
    """What actually crossed the wire.

    ``Content-Length`` on a gzipped response is the compressed length; httpx has already
    decompressed ``content`` by the time we see it, so the header is the only honest source. A
    response without the header (a chunked one) falls back to the decompressed length, which
    over-counts — a gate that guesses high never passes something it should have failed.
    """
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit():
        return int(declared)
    return len(response.content)


def _fetch(client: httpx.Client, url: str) -> tuple[httpx.Response, Asset]:
    response = client.get(url, headers={"Accept-Encoding": "gzip"})
    response.raise_for_status()
    gzipped = "gzip" in response.headers.get("content-encoding", "").lower()
    return response, Asset(
        url=url,
        kind=_kind(url),
        transfer=_transfer_bytes(response),
        raw=len(response.content),
        gzipped=gzipped,
    )


def _subresources(document: str, base: str) -> list[str]:
    found: list[str] = []
    for pattern in (_HREF, _SRC, _IMG):
        for reference in pattern.findall(document):
            absolute = urljoin(base, reference)
            if absolute not in found:
                found.append(absolute)
    return found


def _external_hosts(body: str, origin: str) -> set[str]:
    own = urlsplit(origin).hostname or ""
    return {
        host for host in _EXTERNAL.findall(body) if host.lower() != own.lower() and host.lower() not in _NAMESPACE_HOSTS
    }


ClientFactory = Callable[[], httpx.Client]


def _default_client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT_S, follow_redirects=True)


def measure(
    base_url: str, profile: str, client_factory: ClientFactory | None = None
) -> tuple[list[Asset], list[Breach]]:
    """Fetch `/today` for one profile and everything it references.

    ``client_factory`` exists so the acceptance tests can point the same checker at an in-process
    app — including one deliberately built without ``GZipMiddleware``, which is the only way to
    prove the gate actually fires rather than merely passing on a page that happens to be small.
    """
    page = urljoin(base_url, f"/today?profile={profile}")
    breaches: list[Breach] = []
    assets: list[Asset] = []
    with (client_factory or _default_client)() as client:
        document, asset = _fetch(client, page)
        assets.append(asset)
        bodies = [document.text]
        for url in _subresources(document.text, page):
            sub_response, sub_asset = _fetch(client, url)
            assets.append(sub_asset)
            bodies.append(sub_response.text)

    for asset in assets:
        # A tiny response is not worth compressing and the middleware skips it; a large one that
        # arrives raw means the middleware is gone, which is the failure this gate exists for.
        if not asset.gzipped and asset.transfer > 1024:
            breaches.append(Breach(profile, f"{asset.url} arrived without Content-Encoding: gzip"))

    for body in bodies:
        for host in _external_hosts(body, base_url):
            breaches.append(Breach(profile, f"references the external host {host!r}"))
        if _WEBFONT.search(body):
            breaches.append(Breach(profile, "declares a webfont or a preconnect (system stack only)"))

    for kind, ceiling in BUDGETS.items():
        if kind == "total":
            continue
        used = sum(asset.transfer for asset in assets if asset.kind == kind)
        if used > ceiling:
            breaches.append(Breach(profile, f"{kind} is {used} B, over its {ceiling} B ceiling"))

    total = sum(asset.transfer for asset in assets)
    if total > BUDGETS["total"]:
        breaches.append(Breach(profile, f"the page is {total} B, over the {BUDGETS['total']} B gate"))
    return assets, breaches


def check(base_url: str, client_factory: ClientFactory | None = None) -> list[Breach]:
    """Every breach across both profiles. An empty list is a pass."""
    breaches: list[Breach] = []
    for profile in PROFILES:
        breaches.extend(measure(base_url, profile, client_factory)[1])
    return breaches


def _table(profile: str, assets: list[Asset]) -> str:
    lines = [f"\n/today?profile={profile}", f"  {'transfer':>9}  {'raw':>9}  gz   asset"]
    for asset in assets:
        mark = "yes" if asset.gzipped else "NO "
        lines.append(f"  {asset.transfer:>9}  {asset.raw:>9}  {mark}  {urlsplit(asset.url).path}")
    total = sum(asset.transfer for asset in assets)
    lines.append(f"  {total:>9}  {sum(a.raw for a in assets):>9}       TOTAL (gate {BUDGETS['total']} B)")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    base_url = argv[1] if len(argv) > 1 else "http://localhost:8090"
    breaches: list[Breach] = []
    for profile in PROFILES:
        assets, found = measure(base_url, profile)
        print(_table(profile, assets))
        breaches.extend(found)
    if breaches:
        print("\nBUDGET BREACHED:")
        for breach in breaches:
            print(f"  - {breach}")
        return 1
    print("\nwithin budget.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
