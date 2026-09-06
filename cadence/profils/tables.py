"""The ``profile`` and ``setting`` tables.

PRP-01 owns the tables and ``age_band()``; PRP-03 owns the services and screens over them
(D-023). Columns are ``docs/architecture.md`` section 3 plus the three additions of D-050.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel

from cadence.schema.enums import AgeBand

PROFILE_ME = "me"
PROFILE_SON = "son"


class Profile(SQLModel, table=True):
    __tablename__ = "profile"

    # Slug, not a surrogate key: "me" and "son" are the two profiles and the URLs say so.
    id: str = Field(primary_key=True)
    display_name: str
    kind: str = Field(default="adult")
    age_years: int | None = Field(default=None)
    age_recorded_on: str | None = Field(default=None)
    vitalforge_person: str = Field(default="")
    push_to_garmin: bool = Field(default=False)
    # D-050. The percentage caps of principles section 3.3 are uncomputable without a bodyweight,
    # and an unknown one falls back to the absolute cap rather than to no cap at all.
    bodyweight_kg: float | None = Field(default=None)
    # A profile setting, never an equipment id, and never in the equipment picker (section 1.7).
    has_overhead_anchor: bool = Field(default=False)
    # Denormalised cache of age_band(); recomputed on every write, never the source of truth.
    age_band: str = Field(default=AgeBand.U10.value)


class Setting(SQLModel, table=True):
    __tablename__ = "setting"

    key: str = Field(primary_key=True)
    value_json: str
    updated_at: str
