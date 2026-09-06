"""The ``metrics_cache`` table (``docs/architecture.md`` section 3).

PRP-06 owns filling it from VitalForge; PRP-04 only reads it, so this build creates it empty and
renders "No data yet." until something writes a row. The table lives here rather than in
``cadence/historique/`` because architecture section 2 puts the VitalForge cache in this module,
and PRP-06 should find it where it expects it (D-101).

``payload_json`` shape is fixed by D-101 and is exactly the ``trend`` object of PRP-04's
``GET /api/scorecard``::

    {"weight_kg": [["2026-08-08", 85.2], ...], "body_fat_pct": [["2026-08-08", 19.1], ...]}

Extra keys are ignored, so PRP-06 may cache readiness and the rest alongside these two series.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel


class MetricsCache(SQLModel, table=True):
    __tablename__ = "metrics_cache"

    # One row per profile: the latest pull, not a history table. The series live in the payload.
    profile_id: str = Field(primary_key=True)
    fetched_at: str
    payload_json: str
    # True when the last pull failed and this is the previous answer being served on.
    stale: bool = Field(default=False)
