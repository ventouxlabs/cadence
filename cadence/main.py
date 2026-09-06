"""The FastAPI application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from cadence.api import health
from cadence.config import Settings, get_settings
from cadence.db import init_db

# Later PRPs append one line each.
ROUTERS: list[APIRouter] = [health.router]


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Passing ``settings`` overrides the cached process-wide instance.

    No auth middleware: Cadence is reachable only over Tailscale (D-009).
    """
    active = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db(active)
        yield

    app = FastAPI(title="Cadence", version="0.1.0", lifespan=lifespan)
    app.state.settings = active
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: active
    for router in ROUTERS:
        app.include_router(router)
    return app


app = create_app()
