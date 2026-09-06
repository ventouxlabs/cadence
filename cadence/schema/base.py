"""Shared model configuration for the library DSL.

Every schema model is frozen and forbids extra keys. ``extra="forbid"`` is what makes an
imported YAML carrying a stray key fail closed instead of silently dropping it.

``frozen=True`` alone is shallow - it blocks attribute assignment but leaves a ``list`` or
``dict`` field mutable in place. So the DSL uses tuples for sequences and ``FrozenMap`` for
mappings, and every numeric field is strict: plain ``int`` accepts YAML's ``true`` as ``1`` and
``"12"`` as twelve, which is exactly how a load cap ends up read off a value nobody wrote.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, PlainSerializer, StringConstraints

_K = TypeVar("_K")
_V = TypeVar("_V")


def _freeze_mapping(value: Any) -> Any:
    return MappingProxyType(dict(value)) if isinstance(value, Mapping) else value


FrozenMap = Annotated[
    Mapping[_K, _V],
    AfterValidator(_freeze_mapping),
    PlainSerializer(dict, return_type=dict),
]
"""A mapping field that cannot be written through after validation."""

SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"

Slug = Annotated[str, StringConstraints(pattern=SLUG_PATTERN, min_length=1, max_length=64)]


class CadenceModel(BaseModel):
    """Base for every library DSL model."""

    model_config = ConfigDict(extra="forbid", frozen=True)
