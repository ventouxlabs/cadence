"""The gates in front of the validator: size, shape, and hostile content.

Everything here runs on text nobody in this household wrote. Two rules shape the module:

1. **The scan runs twice.** Once on the raw bytes-as-text before the parser sees them, and again
   on every string the parser produced. A payload can hide a template delimiter from the first
   pass behind a YAML escape (``"\\x7b\\x7b"``) and hide it from the second behind nothing at all,
   so neither pass is redundant.
2. **Nothing here reads the filesystem, and nothing here builds HTML.** The service is handed a
   ``str`` and answers with findings; escaping is Jinja's job (D-010, PRP-08 risks 4, 5, 8).
"""

from __future__ import annotations

import re
from typing import Any

import yaml

# Pipeline codes (PRP-08). Validator codes are never re-declared here: they pass through verbatim
# from ``cadence.validateur.codes`` so one rule is never reported under two names.
PARSE_ERROR = "parse_error"
NOT_A_MAPPING = "not_a_mapping"
TOO_LARGE = "too_large"
TOO_MANY_EXERCISES = "too_many_exercises"
STRING_TOO_LONG = "string_too_long"
SUSPICIOUS_CONTENT = "suspicious_content"
ID_COLLISION = "id_collision"
INLINE_EXERCISE_NOT_ALLOWED_FOR_YOUTH = "inline_exercise_not_allowed_for_youth"
VALIDATION_FAILED = "validation_failed"
GENERATION_FAILED = "generation_failed"
GENERATION_UNAVAILABLE = "generation_unavailable"

PIPELINE_CODES: frozenset[str] = frozenset(
    {
        PARSE_ERROR,
        NOT_A_MAPPING,
        TOO_LARGE,
        TOO_MANY_EXERCISES,
        STRING_TOO_LONG,
        SUSPICIOUS_CONTENT,
        ID_COLLISION,
        INLINE_EXERCISE_NOT_ALLOWED_FOR_YOUTH,
        VALIDATION_FAILED,
        GENERATION_FAILED,
        GENERATION_UNAVAILABLE,
    }
)

MAX_BODY_BYTES = 256 * 1024
MAX_EXERCISES = 30
MAX_DEPTH = 6

# PRP-00's model bounds, restated as pre-parse caps so a 40 KB name is refused by the gate that
# owns readable messages rather than by a Pydantic error nobody can act on.
MAX_STRING = 500
FIELD_LIMITS: dict[str, int] = {
    "id": 64,
    "name": 100,
    "cue": 120,
    "cue_override": 120,
    "variant": 64,
    "notes": MAX_STRING,
}

# Anchors, aliases and tags are found with PyYAML's own scanner rather than with a regex. A regex
# has to guess where a node begins, and the first draft's did: it required a word character after
# the sigil, so ``&-a`` and ``*-a`` - both legal anchor names, both alias-expansion vectors -
# walked straight through it. The scanner knows the grammar, and it only *tokenises*: aliases are
# expanded at composition, not here, so a document full of them costs one pass and no memory.
_YAML_TOKEN_NAMES: dict[type, str] = {
    yaml.tokens.AnchorToken: "a YAML anchor",
    yaml.tokens.AliasToken: "a YAML alias",
    yaml.tokens.TagToken: "a YAML tag",
}

# Everything whose mere presence condemns a *string*, raw document or parsed field alike. ``!!``
# is deliberately not here any more: it is a tag in document text, which the scanner catches
# precisely, and it is an exclamation in a cue, which nobody should be refused for writing.
_LITERAL_TOKENS: tuple[str, ...] = (
    "http://",
    "https://",
    "{{",
    "{%",
    "{#",
    "<script",
    "javascript:",
    "data:text/html",
)

# C0 and DEL, minus the three a text document legitimately contains.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Finding:
    """One pipeline finding, shaped like a validator finding so the two lists concatenate.

    A plain class rather than a Pydantic model: this is built inside tight loops on a rejection
    path, and it never crosses a validation boundary - ``as_dict`` is the only way out.
    """

    __slots__ = ("code", "message", "path", "severity")

    def __init__(self, code: str, message: str, path: str = "$", severity: str = "error") -> None:
        self.code = code
        self.message = message
        self.path = path
        self.severity = severity

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "message": self.message, "row": row_of(self.path)}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Finding({self.code!r}, {self.path!r})"


_ROW_PATH = re.compile(r"rows\[(\d+)\]")


def row_of(path: str) -> int | None:
    """The 1-based row number a path points at, or ``None`` for a document-level finding.

    One-based because the number is read by a person looking at a list on a phone, and the
    wireframe says "row 2". The zero-based index stays visible in ``path``.
    """
    found = _ROW_PATH.search(path or "")
    return int(found.group(1)) + 1 if found else None


def scan_text(text: str, *, path: str = "$") -> list[Finding]:
    """Reject a string carrying a hostile token. Used on raw text and on parsed fields alike.

    The message names the token and never echoes the document: an error line is rendered back to
    the browser, and quoting the payload into it would hand the attacker the output channel.
    """
    lowered = text.lower()
    for token in _LITERAL_TOKENS:
        if token in lowered:
            return [_suspicious(token, path)]
    if _CONTROL_CHARS.search(text):
        return [_suspicious("a control character", path)]
    return []


def scan_document(text: str, *, path: str = "$") -> list[Finding]:
    """``scan_text`` plus the YAML grammar checks that only make sense on document text.

    Anchors and aliases are structure, so they are looked for here and not in ``scan_parsed``:
    by the time a string has come back from ``safe_load`` any alias in it has already been
    expanded, and a surviving ``*a`` is a cue somebody wrote.
    """
    if found := scan_text(text, path=path):
        return found
    return _scan_yaml_tokens(text, path)


def _scan_yaml_tokens(text: str, path: str) -> list[Finding]:
    """One tokenising pass. A document that will not even tokenise is left to the parser.

    Returning nothing on a scan error is deliberate: ``yaml.scan`` raises on ``"::: not yaml"``
    and on a multi-document stream, and both of those are ``parse_error`` reported by the parse
    stage. Reporting them here as suspicious content would name the wrong problem.
    """
    try:
        for token in yaml.scan(text, Loader=yaml.SafeLoader):
            name = _YAML_TOKEN_NAMES.get(type(token))
            if name is not None:
                return [_suspicious(name, path)]
    except (yaml.YAMLError, RecursionError, ValueError):
        return []
    return []


def _suspicious(token: str, path: str) -> Finding:
    return Finding(
        SUSPICIOUS_CONTENT,
        f"this document contains {token!r}, which a workout never needs; it was not imported",
        path,
    )


def scan_parsed(value: Any, *, path: str = "$") -> list[Finding]:
    """The second pass: every string the parser produced, wherever it sits in the tree."""
    findings: list[Finding] = []
    for where, text in _walk_strings(value, path):
        findings.extend(scan_text(text, path=where))
    return findings


def check_string_lengths(value: Any, *, path: str = "$") -> list[Finding]:
    """PRP-00's model bounds, applied by key name and with a blanket ceiling for everything else."""
    findings: list[Finding] = []
    for where, text in _walk_strings(value, path):
        key = where.rsplit(".", 1)[-1]
        limit = FIELD_LIMITS.get(key, MAX_STRING)
        if len(text) > limit:
            findings.append(
                Finding(
                    STRING_TOO_LONG,
                    f"{key} is {len(text)} characters; the limit is {limit}",
                    where,
                )
            )
    return findings


def _walk_strings(value: Any, path: str) -> list[tuple[str, str]]:
    """Every string in the tree with the path that reaches it. Mapping keys are scanned too."""
    found: list[tuple[str, str]] = []
    stack: list[tuple[str, Any]] = [(path, value)]
    while stack:
        where, node = stack.pop()
        if isinstance(node, str):
            found.append((where, node))
        elif isinstance(node, dict):
            for key, item in node.items():
                name = str(key)
                if isinstance(key, str):
                    found.append((f"{where}.<key>", name))
                stack.append((f"{where}.{name}" if where != "$" else name, item))
        elif isinstance(node, list | tuple):
            for index, item in enumerate(node):
                stack.append((f"{where}[{index}]", item))
    return found


def bracket_depth(text: str, limit: int = MAX_DEPTH) -> int:
    """How deeply a JSON *text* nests, measured without parsing it.

    ``json.loads`` and ``json.dumps`` are both recursive, so a body of ten thousand open brackets
    raises ``RecursionError`` from inside the standard library - which is a 500 on a request that
    should have been a 422 (Codex finding 9). Counting brackets is one linear pass with no stack
    at all, and it runs before anything recursive sees the text. String contents are skipped, so a
    cue full of braces costs nothing.
    """
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
            if deepest > limit:
                return deepest
        elif char in "]}":
            depth -= 1
    return deepest


def check_json_depth(text: str, limit: int = MAX_DEPTH) -> list[Finding]:
    """The gate in front of every ``json.loads`` on an untrusted body."""
    depth = bracket_depth(text, limit)
    if depth <= limit:
        return []
    return [Finding(PARSE_ERROR, f"this body nests {depth} levels deep; the limit is {limit}")]


def depth_of(value: Any, limit: int = MAX_DEPTH) -> int:
    """How deeply the document nests, counted iteratively and abandoned once past ``limit``.

    Iterative on purpose: a recursive measure on a deliberately deep document is a
    ``RecursionError`` in the middle of a request, which is the denial of service the depth cap
    exists to prevent.
    """
    deepest = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, level = stack.pop()
        deepest = max(deepest, level)
        if level > limit:
            return deepest
        if isinstance(node, dict):
            stack.extend((item, level + 1) for item in node.values())
        elif isinstance(node, list | tuple):
            stack.extend((item, level + 1) for item in node)
    return deepest


def check_depth(value: Any, limit: int = MAX_DEPTH) -> list[Finding]:
    depth = depth_of(value, limit)
    if depth <= limit:
        return []
    return [
        Finding(
            PARSE_ERROR,
            f"this document nests {depth} levels deep; the limit is {limit}",
        )
    ]
