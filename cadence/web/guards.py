"""Two guards in front of every route that writes: a size cap and a same-origin check.

They are applied two different ways, and the difference is not stylistic. **FastAPI reads the
request body before it solves dependencies** whenever the path operation declares a ``Form`` or
``File`` parameter, so a route-level dependency on such a route runs *after* the multipart parser
has already spooled the upload it was meant to refuse. Hence:

- ``require_same_origin`` is a router dependency. Parsing a body is harmless; what matters is that
  nothing is written, and the dependency is resolved before the handler body runs either way.
- ``enforce_declared_size`` is a router dependency **and** is called directly by
  ``read_bounded_form``. On a handler that takes a bare ``Request`` (which is why
  ``/api/import`` and ``/settings/import`` both do) that call is the one that runs in time.

``RequestRefused`` is answered by one handler registered in ``main.py``, so a refusal reaches the
JSON API in the envelope and the Settings screen as the partial that panel expects (D-171).
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.datastructures import FormData
from fastapi.responses import HTMLResponse, JSONResponse, Response

from cadence.api.envelope import err
from cadence.bibliotheque import untrusted
from cadence.bibliotheque.untrusted import MAX_BODY_BYTES, Finding

logger = logging.getLogger(__name__)

UNPROCESSABLE = 422

# Which panel a refused Settings post is rendered back into. Keyed by path because the refusal
# happens before the handler that would otherwise have said.
_PANEL_FOR_PREFIX: tuple[tuple[str, str], ...] = (
    ("/settings/import", "import_result"),
    ("/settings/generate", "generate_preview"),
)

# The methods that change something. A GET carries no CSRF risk worth a same-origin check, and
# refusing one would break a bookmarked link.
UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RequestRefused(Exception):
    """A request refused before its body was read. Carries the code the envelope reports."""

    def __init__(self, code: str, message: str, path: str = "$") -> None:
        super().__init__(message)
        self.finding = Finding(code, message, path)
        self.code = code


def enforce_declared_size(request: Request) -> None:
    """A declared size over the cap is refused whatever the content type; multipart must declare one.

    Content-Length is the only bound available *before* the parser runs, and Starlette's multipart
    parser spools a large part to a temporary file as it goes - so a chunked upload with no
    declared length filled a disk with the size gate standing right behind it (Codex finding 2).
    A browser always sends Content-Length for a form post; a client that will not say how big its
    upload is has to be refused rather than measured afterwards.

    The two halves are deliberately not the same rule (D-193):

    - **Absent** Content-Length is refused for multipart only. The JSON path streams under a
      running total in ``read_capped`` and needs no declared length, and requiring one there would
      refuse a chunked client the app has no reason to refuse.
    - **Present and over the cap** is refused for *every* content type. The earlier version
      returned early for anything that was not multipart, which left a urlencoded form post - the
      shape the Settings cards actually use - with no declared-size gate at all.
    """
    if request.method not in UNSAFE_METHODS:
        return
    declared = request.headers.get("content-length")
    is_multipart = request.headers.get("content-type", "").startswith("multipart/form-data")
    if declared is None or not declared.isdigit():
        if is_multipart:
            raise RequestRefused(
                untrusted.TOO_LARGE,
                "this upload did not say how big it is; send it with a Content-Length header",
            )
        return
    if int(declared) > MAX_BODY_BYTES:
        raise RequestRefused(untrusted.TOO_LARGE, f"a workout must be under {MAX_BODY_BYTES // 1024} KB")


_DEFAULT_PORTS = frozenset({80, 443})
_WEB_SCHEMES = frozenset({"http", "https"})


def _authority(value: str, *, is_url: bool) -> tuple[str, int | None]:
    """The comparable ``(host, port)`` of an ``Origin``/``Referer`` URL or a ``Host`` authority.

    Raises ``ValueError`` for anything that is not plainly one: ``null``, an empty value, no
    scheme, a scheme that is not http(s), userinfo, a bad port. The
    caller treats that as a refusal: a header that is present and unreadable is not "absent".

    Case is folded, one trailing dot is dropped (``example.com.`` is ``example.com``), and the
    default ports 80 and 443 are dropped so ``https://h`` equals ``Host: h:443``. Hosts are compared
    whole, never by suffix, so ``evil.example.com`` and ``example.com.evil.net`` never match.
    """
    value = value.strip()
    if not value:
        raise ValueError("empty")
    split = urlsplit(value if is_url else f"//{value}")
    if is_url and (split.scheme.lower() not in _WEB_SCHEMES or not split.netloc):
        raise ValueError("not an http(s) URL")
    if "@" in split.netloc:
        raise ValueError("userinfo")
    host = (split.hostname or "").rstrip(".")
    if not host:
        raise ValueError("no host")
    port = split.port  # ValueError on a non-numeric or out-of-range port
    return host, None if port in _DEFAULT_PORTS else port


def require_same_origin(request: Request) -> None:
    """Refuse a state-changing request whose ``Origin`` or ``Referer`` names another site.

    Cadence has no login and no session cookie: it is reachable over Tailscale only (D-009), and
    every request that arrives is authorised by having arrived. That is exactly what makes a
    cross-site form post interesting - a page on any other tab can POST here, and the browser will
    send it. Comparing the declared origin against the host is the check that costs nothing and
    needs no token (D-186).

    A header that is *absent* is not evidence of anything: ``curl`` sends neither, and so does the
    service worker's replay. A header that is *present* must name this host: ``Origin: null`` (what
    a sandboxed iframe or a cross-origin redirect sends) and anything unparseable are refused, and
    so is a repeated header with any value that is foreign.

    The guard is installed app-wide in ``create_app``, so a new route cannot be left without it.
    """
    if request.method not in UNSAFE_METHODS:
        return
    # Browsers always send Sec-Fetch-Site and a page cannot forge it; non-browser clients omit it.
    # Only "cross-site" is refused: the other values (and none at all) go on to the Origin check.
    # Scheme is deliberately not compared anywhere here: behind a TLS-terminating proxy the app
    # sees http while Origin says https. Known limit: a network attacker could downgrade a scheme.
    # Refusals are 422, not 403: the envelope and the Settings partial depend on it, so monitoring
    # sees CSRF blocks as 422.
    cross_site = request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site"
    declared = [*request.headers.getlist("origin"), *request.headers.getlist("referer")]
    if not declared and not cross_site:
        return
    try:
        host = _authority(request.headers.get("host", ""), is_url=False)
        foreign = cross_site or any(_authority(value, is_url=True) != host for value in declared)
    except ValueError:
        foreign = True
    if foreign:
        logger.warning("refused a cross-site %s to %s", request.method, request.scope.get("path"))
        raise RequestRefused(
            untrusted.PARSE_ERROR,
            "this request came from another site and was not carried out",
        )


async def read_bounded_form(request: Request) -> FormData:
    """The size check, then the parse. In that order, and in the handler rather than beside it.

    A route-level dependency is *not* early enough for a path operation that declares ``Form`` or
    ``File`` parameters: FastAPI reads the body before it solves dependencies, so the guard would
    run after the spooling it exists to prevent. A handler that takes a bare ``Request`` and calls
    this is the only shape where the order is the one the docstring claims (Codex finding 2).
    """
    enforce_declared_size(request)
    return await request.form(max_part_size=MAX_BODY_BYTES)


async def refusal_response(request: Request, exc: Exception) -> Response:
    """One refusal, two shapes: the envelope for the API, the panel for the Settings screen."""
    refused = exc if isinstance(exc, RequestRefused) else RequestRefused(untrusted.PARSE_ERROR, "refused")
    body = {"errors": [refused.finding.as_dict()]}
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=UNPROCESSABLE, content=err(refused.code, data=body))
    for prefix, panel in _PANEL_FOR_PREFIX:
        if request.url.path.startswith(prefix):
            from cadence.web.rendering import templates

            html = templates.get_template(f"partials/{panel}.html").render(
                request=request, result={"ok": False, "errors": body["errors"]}, preview=None
            )
            return HTMLResponse(html, status_code=UNPROCESSABLE)
    return JSONResponse(status_code=UNPROCESSABLE, content=err(refused.code, data=body))


__all__ = [
    "UNSAFE_METHODS",
    "RequestRefused",
    "enforce_declared_size",
    "read_bounded_form",
    "refusal_response",
    "require_same_origin",
]
