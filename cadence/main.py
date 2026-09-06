"""The FastAPI application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from cadence.api import health, sessions
from cadence.api import today as api_today
from cadence.api.envelope import err
from cadence.config import Settings, get_settings
from cadence.db import init_db
from cadence.web.rendering import STATIC_DIR
from cadence.web.routers import pwa
from cadence.web.routers import today as web_today

# Later PRPs append one line each.
ROUTERS: list[APIRouter] = [
    health.router,
    api_today.router,
    sessions.router,
    web_today.router,
    pwa.router,
]

# D-025: the one middleware in this app. Vendored HTMX is ~50 KB raw and ~17 KB gzipped, so the
# 60 KB budget for /today (architecture section 5) is unreachable without it. 500 bytes is the
# floor below which compressing costs more than it saves.
GZIP_MINIMUM_SIZE = 500


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Passing ``settings`` overrides the cached process-wide instance.

    No auth middleware: Cadence is reachable only over Tailscale (D-009). ``GZipMiddleware``
    is the one exception and exists for the page-weight budget alone (D-025).
    """
    active = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db(active)
        yield

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

    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MINIMUM_SIZE)
    app.state.settings = active
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: active
    for router in ROUTERS:
        app.include_router(router)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()
