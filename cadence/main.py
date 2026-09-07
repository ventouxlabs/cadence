"""The FastAPI application."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.gzip import GZipMiddleware

from cadence.api import assessments as api_assessments
from cadence.api import generate as api_generate
from cadence.api import health, mock_inspect, profiles, sessions
from cadence.api import history as api_history
from cadence.api import import_ as api_import
from cadence.api import metrics as api_metrics
from cadence.api import settings as api_settings
from cadence.api import sync as api_sync
from cadence.api import today as api_today
from cadence.api.envelope import err
from cadence.config import MOCK_MODE, Settings, get_settings
from cadence.db import init_db
from cadence.vitalforge.periodic import periodic_sync
from cadence.web.gate import SetupRequired, require_setup, setup_redirect
from cadence.web.guards import RequestRefused, refusal_response
from cadence.web.rendering import STATIC_DIR
from cadence.web.routers import assess as web_assess
from cadence.web.routers import history as web_history
from cadence.web.routers import pwa
from cadence.web.routers import settings as web_settings
from cadence.web.routers import settings_ai as web_settings_ai
from cadence.web.routers import today as web_today

# Later PRPs append one line each.
ROUTERS: list[APIRouter] = [
    health.router,
    api_today.router,
    api_settings.router,
    profiles.router,
    sessions.router,
    api_history.router,
    api_assessments.router,
    api_import.router,
    api_generate.router,
    api_metrics.router,
    api_sync.router,
    web_settings.router,
    web_settings_ai.router,
    pwa.router,
]

# HTML screens that must not render before the son's age has been asked for (PRP-03 risk 7). The
# gate is registered here rather than inside each handler so a route added later is covered by
# being on this list, not by somebody remembering. `/setup` itself and the JSON API are not on it.
#
# PRP-04's History is on it for the same reason Today is: it renders the son's sessions, and a
# profile whose age has never been asked for has been training against the strictest defaults, so
# the screen would be showing work done under rules nobody chose.
GATED_ROUTERS: list[APIRouter] = [web_today.router, web_history.router, web_assess.router]

# D-025: the one middleware in this app. Vendored HTMX is ~50 KB raw and ~17 KB gzipped, so the
# 60 KB budget for /today (architecture section 5) is unreachable without it. 500 bytes is the
# floor below which compressing costs more than it saves.
GZIP_MINIMUM_SIZE = 500

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Passing ``settings`` overrides the cached process-wide instance.

    No auth middleware: Cadence is reachable only over Tailscale (D-009). ``GZipMiddleware``
    is the one exception and exists for the page-weight budget alone (D-025).
    """
    active = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = init_db(active)
        _refresh_bands(engine)
        # The one background task in the app (architecture section 1): no worker process, just a
        # loop that drains the VitalForge queue and refreshes the metrics cache every five
        # minutes. It sleeps before its first pass, so starting the app never costs a request.
        #
        # Off under ``CADENCE_ENV=test`` and under ``CADENCE_PERIODIC_SYNC=0``: a loop that wakes
        # up mid-assertion writes to the same rows the test is reading, which is the difference
        # between a suite that fails honestly and one that fails on Tuesdays (D-140).
        if not active.periodic_sync_enabled:
            logger.info("the periodic VitalForge sync is off (env=%s)", active.env)
            yield
            return
        task = asyncio.create_task(periodic_sync(active))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Cadence", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _invalid_body(request: Request, exc: RequestValidationError) -> JSONResponse:
        """A malformed body answers in the envelope, and never echoes the value back.

        FastAPI's default handler puts the offending input in its response, and no JSON encoder
        will serialise the ``NaN`` a hand-rolled offline client can send: the row endpoint
        answered 500 with a plain-text body, which wedges the replay queue for good, because
        ``app.js`` retries on 5xx and stops on the first failure.
        """
        fields = ", ".join(".".join(str(part) for part in error["loc"][1:]) or "body" for error in exc.errors())
        return JSONResponse(status_code=422, content=err(f"this request body is not valid: {fields}"))

    app.add_exception_handler(SetupRequired, setup_redirect)
    # A request refused before its body was read: too large to accept, or from another
    # site. Answered in the envelope for /api and as the panel's partial for Settings.
    app.add_exception_handler(RequestRefused, refusal_response)
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_SIZE)
    app.state.settings = active
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: active
    for router in ROUTERS:
        app.include_router(router)
    # Mock-mode only, and never under prod. The fake's recorder is the one place that can say
    # whether a session reported as synced actually produced one activity and not zero or two,
    # which is what PRP-09's smoke test asserts (D-169). Settings validation already refuses
    # mock under prod; the env check here means the route cannot appear even if that changes.
    if active.vitalforge_mode == MOCK_MODE and active.env != "prod":
        app.include_router(mock_inspect.router)
    for router in GATED_ROUTERS:
        app.include_router(router, dependencies=[Depends(require_setup)])
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def _refresh_bands(engine: Any) -> None:
    """Recompute the cached age bands on boot (PRP-03 risk 3).

    A son who has a birthday between two restarts would otherwise be judged against last year's
    band until somebody happened to open his profile. A database that will not answer must not
    stop the app from starting: the band is a cache, and ``age_band()`` recomputes it on read.
    """
    from sqlmodel import Session as DbSession

    from cadence.profils.services import refresh_age_bands

    try:
        with DbSession(engine) as session:
            refresh_age_bands(session)
    except SQLAlchemyError:
        logger.exception("could not refresh the cached age bands at startup; they are recomputed on read")


app = create_app()
