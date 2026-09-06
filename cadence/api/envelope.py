"""The one response shape every ``/api/*`` route returns (``docs/architecture.md`` section 4)."""

from __future__ import annotations

from typing import Any

ENVELOPE_KEYS: frozenset[str] = frozenset({"ok", "data", "error", "meta"})


def ok(data: Any = None, **meta: Any) -> dict[str, Any]:
    """A successful response."""
    return {"ok": True, "data": data, "error": None, "meta": dict(meta)}


def err(message: str, *, data: Any = None, **meta: Any) -> dict[str, Any]:
    """A failed response. ``message`` is user-facing and must never carry a secret."""
    return {"ok": False, "data": data, "error": message, "meta": dict(meta)}
