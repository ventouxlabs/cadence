"""The structural half of solo mode: every screen with a tab strip is actually wired to it.

``cadence.web.rendering.profile_tabs`` falls back to all three tabs for a request that never met
``require_visible_profile``. That fallback is deliberate — it is the behaviour the app had before
the setting existed, and a missing tab is a better failure than a crashed page — but it fails
*silently*, which means a screen added later in a router nobody registered would quietly start
offering the son again on a household that has hidden him.

So the wiring is asserted rather than trusted, the way ``test_config_parity`` and
``test_principles_parity`` assert theirs. Two questions: does every full-page template belong to a
registered router, and does every route on those routers actually carry the dependency (D-262).
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute

from cadence.main import TABBED_ROUTERS, create_app
from cadence.web.solo import require_visible_profile

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "cadence"
TEMPLATE_DIR = PACKAGE / "web" / "templates"
ROUTER_DIR = PACKAGE / "web" / "routers"

EXTENDS_BASE = re.compile(r'{%-?\s*extends\s+"base\.html"\s*-?%}')

# Routers that render no page with a header, and so need no tab strip and no database read.
# ``settings_ai`` answers with partials only; ``pwa`` serves ``/sw.js`` and ``/manifest.json``,
# which the browser fetches for itself.
NO_PAGES: frozenset[str] = frozenset({"settings_ai.py", "pwa.py"})

# The only modules allowed to render a template with a tab strip in it. Every one of them owns a
# router on ``TABBED_ROUTERS``, which is what the second test below checks; this list is the
# first half of the same claim, and it is a whitelist rather than a scan of one directory so a
# page rendered from *anywhere* in the package has to be added here deliberately (D-274).
# ``web/guards.py`` renders outside the routers today and is absent on purpose: it answers a
# refusal with a partial, never a page.
PAGE_RENDERERS: frozenset[str] = frozenset({"today.py", "history.py", "assess.py", "settings.py"})


def _full_page_templates() -> set[str]:
    """Every template that extends ``base.html``, and so renders the tab strip.

    ``rglob`` rather than ``glob``: a full page living under ``partials/`` would be a misfiled
    page, not an exemption, and the scan has to see it either way.
    """
    return {path.name for path in TEMPLATE_DIR.rglob("*.html") if EXTENDS_BASE.search(path.read_text())}


def _modules_naming_a_page() -> dict[str, str]:
    """Every module anywhere in ``cadence/`` that names a full-page template."""
    pages = _full_page_templates()
    found: dict[str, str] = {}
    for path in PACKAGE.rglob("*.py"):
        source = path.read_text()
        if any(page in source for page in pages):
            found[path.name] = source
    return found


def _router_modules() -> dict[str, str]:
    return {path.name: path.read_text() for path in ROUTER_DIR.glob("*.py") if path.name != "__init__.py"}


def test_no_module_outside_the_registered_routers_renders_a_page() -> None:
    """The scan covers the whole package, not one directory (D-274).

    ``profile_tabs`` falls back to all three tabs when the dependency has not run, and a page
    rendered from a module with no router — a helper, an exception handler, a background task —
    would take that fallback and quietly show the son to a household that has hidden him. Scanning
    only ``web/routers/`` could not have seen it.
    """
    assert set(_modules_naming_a_page()) == PAGE_RENDERERS


def test_the_exempt_routers_still_render_no_page() -> None:
    """``NO_PAGES`` is an assertion, not a hole.

    Excluding these two from the scan and leaving it there would exempt exactly the routers where
    a new full-page screen could appear undetected — which is the failure the scan exists to
    catch. So they are scanned too, with the opposite expectation.
    """
    pages = _full_page_templates()
    for name in NO_PAGES:
        source = (ROUTER_DIR / name).read_text()
        found = [page for page in pages if page in source]
        assert not found, (
            f"{name} now renders {found}, a template with a tab strip, so it must join "
            "main.TABBED_ROUTERS and come off NO_PAGES"
        )


def test_the_templates_this_test_polices_are_actually_there() -> None:
    """A guard on the guard: an empty set would make every assertion below pass vacuously."""
    pages = _full_page_templates()
    assert {"today.html", "history.html", "settings.html", "done.html"} <= pages
    assert "base.html" not in pages


def test_every_router_that_renders_a_page_is_registered_for_the_tab_strip() -> None:
    """A full-page template named in a router module that is not on ``TABBED_ROUTERS`` is the bug.

    The module is imported by name rather than looked up in a table written here, so a router
    added tomorrow is checked by existing rather than by being added to a second list.
    """
    import importlib

    pages = _full_page_templates()
    checked = 0
    for name, source in _router_modules().items():
        if not any(page in source for page in pages):
            continue
        router = importlib.import_module(f"cadence.web.routers.{name.removesuffix('.py')}").router
        assert any(router is item for item in TABBED_ROUTERS), (
            f"{name} renders a page with a tab strip but is not on main.TABBED_ROUTERS, so its "
            "tabs would fall back to showing the son on a household that has hidden him"
        )
        checked += 1
    assert checked >= 4, "the router scan found fewer page-rendering modules than this app has"


def test_every_route_on_a_tabbed_router_carries_the_dependency(settings) -> None:
    """Registered on the router, so this holds for a route added to one of them tomorrow."""
    app = create_app(settings)
    # Counted as (path, method) rather than by path: several of these screens answer a GET and a
    # POST on the same path, and comparing against unique paths would let a route slip through.
    expected = {
        (route.path, method)
        for router in TABBED_ROUTERS
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert expected, "the routers under test define no routes"

    seen: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            if (route.path, method) not in expected:
                continue
            calls = {dependency.call for dependency in route.dependant.dependencies}
            assert require_visible_profile in calls, f"{method} {route.path}"
            seen.add((route.path, method))
    assert seen == expected


def test_the_json_api_never_takes_the_dependency(settings) -> None:
    """``/api/*`` answers in the envelope and must never be handed a redirect (the gate's own rule).

    The same reason ``gate.py`` is a dependency on the HTML routers and not middleware: the service
    worker and the offline queue both read the envelope, and a 303 is not one.
    """
    app = create_app(settings)
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        calls = {dependency.call for dependency in route.dependant.dependencies}
        assert require_visible_profile not in calls, route.path
