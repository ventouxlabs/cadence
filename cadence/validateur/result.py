"""Validation result types and the Pydantic-error flattener."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic import ValidationError as PydanticValidationError

from cadence.validateur import codes


class ValidationError(BaseModel):
    """One finding. ``path`` points at the offending value, ``code`` at the rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    code: str
    message: str
    severity: Literal["error", "warn"] = "error"
    remedy: str | None = None


class ValidationResult(BaseModel):
    """The outcome of one validation pass.

    ``ok`` is ``True`` when nothing of severity ``error`` was found. Warnings do not block.
    Callers must branch on ``.ok`` - this is a Pydantic model and therefore always truthy,
    so ``if validate_workout(...): raise`` would reject every document, valid ones included.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: bool
    errors: list[ValidationError]


def make_error(path: str, code: str, message: str) -> ValidationError:
    """Build a finding, taking severity and remedy from the code table."""
    return ValidationError(
        path=path,
        code=code,
        message=message,
        severity=codes.SEVERITY.get(code, "error"),
        remedy=codes.REMEDY.get(code),
    )


def result_from(errors: list[ValidationError]) -> ValidationResult:
    """Wrap findings; ``ok`` is the absence of any ``error``-severity entry."""
    return ValidationResult(ok=not any(e.severity == "error" for e in errors), errors=list(errors))


def format_path(loc: tuple[object, ...]) -> str:
    """Render a Pydantic ``loc`` as ``rows[3].load_kg``. An empty loc is the document root."""
    if not loc:
        return "$"
    rendered = ""
    for part in loc:
        if isinstance(part, int):
            rendered += f"[{part}]"
        elif rendered:
            rendered += f".{part}"
        else:
            rendered = str(part)
    return rendered or "$"


def flatten_pydantic(exc: PydanticValidationError) -> list[ValidationError]:
    """One ``ValidationError`` per Pydantic error.

    A custom error type that names a known code keeps that code (``measure_conflict``); an
    enum failure on an ``equipment`` field becomes ``equipment_not_whitelisted`` so an
    off-whitelist implement is reported as what it is rather than as a generic parse failure.
    """
    findings: list[ValidationError] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        raw_type = str(err.get("type", codes.SCHEMA))
        if raw_type in codes.ALL_CODES:
            code = raw_type
        elif "equipment" in {str(part) for part in loc}:
            code = codes.EQUIPMENT_NOT_WHITELISTED
        else:
            code = codes.SCHEMA
        findings.append(make_error(format_path(tuple(loc)), code, err.get("msg", "invalid value")))
    return findings
