"""``POST /api/import`` under hostile input - PRP-08 acceptance tests 6 to 13, 18 to 20.

Split from ``test_import.py`` to keep both files inside the 400-line rule. This half is the one
that matters to the Codex adversarial review: parser abuse, injection, and the filesystem.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import yaml
from sqlmodel import Session as DbSession

from cadence.bibliotheque import import_service, untrusted
from cadence.db import get_engine
from cadence.seance.catalog import library_bundle
from cadence.web.rendering import templates
from tests import documents as doc
from tests.test_import import codes_of, stored

LIBRARY_PATH = Path(__file__).resolve().parents[1] / "library"


# -------------------------------------------------------------------------------- hostile input


async def test_import_malformed_text(seeded_client: httpx.AsyncClient) -> None:
    """Test 6: a readable refusal, and no traceback anywhere in the body."""
    response = await seeded_client.post("/api/import", json={"text": "::: not yaml", "profile": "me"})
    assert response.status_code == 422
    assert "parse_error" in codes_of(response.json())
    assert "Traceback" not in response.text
    assert "cadence/" not in response.text


async def test_import_multi_document(seeded_client: httpx.AsyncClient) -> None:
    """Test 7."""
    two = f"{doc.ADULT_WORKOUT}---\n{doc.ADULT_WORKOUT}"
    response = await seeded_client.post("/api/import", json={"text": two, "profile": "me"})
    assert response.status_code == 422
    assert "parse_error" in codes_of(response.json())


async def test_import_too_large(seeded_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 8: 300 KB is refused, and the parser is never reached."""
    calls: list[str] = []
    real = yaml.safe_load

    def spy(stream, *args, **kwargs):  # noqa: ANN001, ANN202
        calls.append("parsed")
        return real(stream, *args, **kwargs)

    monkeypatch.setattr(yaml, "safe_load", spy)
    response = await seeded_client.post(
        "/api/import",
        content=b"x" * (300 * 1024),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422
    assert codes_of(response.json()) == {"too_large"}
    assert calls == []


async def test_import_too_many_exercises(seeded_client: httpx.AsyncClient) -> None:
    """Test 9: thirty-one rows."""
    response = await seeded_client.post("/api/import", json={"text": doc.many_rows(31), "profile": "me"})
    assert response.status_code == 422
    assert "too_many_exercises" in codes_of(response.json())


async def test_import_thirty_exercises_is_the_edge(seeded_client: httpx.AsyncClient) -> None:
    """Thirty is the cap, not one under it. Asserted from both sides so the boundary is pinned.

    Thirty push-up rows is not a *sensible* workout and the validator may well have other things
    to say about it; the only claim here is that the row-count gate is not one of them.
    """
    response = await seeded_client.post("/api/import", json={"text": doc.many_rows(30), "profile": "me"})
    assert "too_many_exercises" not in codes_of(response.json()) if response.status_code == 422 else True
    over = await seeded_client.post("/api/import", json={"text": doc.many_rows(31), "profile": "me"})
    assert "too_many_exercises" in codes_of(over.json())


async def test_import_yaml_alias_bomb(seeded_client: httpx.AsyncClient) -> None:
    """Test 10: ``safe_load`` blocks construction but expands aliases, so anchors never parse."""
    bomb = "id: &a boom\nname: *a\nrows: []\n"
    response = await seeded_client.post("/api/import", json={"text": bomb, "profile": "me"})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"suspicious_content"}


async def test_import_yaml_tag_rejected(seeded_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 11: a python tag never parses, and ``os.system`` is never called."""
    fired: list[str] = []
    monkeypatch.setattr(os, "system", lambda command: fired.append(command) or 0)
    payload = "id: pwn\nname: !!python/object/apply:os.system ['touch /tmp/pwned']\nrows: []\n"
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"suspicious_content"}
    assert fired == []


async def test_import_url_in_cue(seeded_client: httpx.AsyncClient) -> None:
    """Test 12."""
    payload = doc.ADULT_WORKOUT.replace("notes: pasted by hand", "notes: see https://evil.example/x")
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"suspicious_content"}


async def test_import_jinja_delimiters(seeded_client: httpx.AsyncClient) -> None:
    """Test 13."""
    payload = doc.ADULT_WORKOUT.replace("name: My upper", "name: '{{ 7*7 }}'")
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"suspicious_content"}


async def test_import_script_tag_rejected(seeded_client: httpx.AsyncClient) -> None:
    """``<script`` never reaches storage at all, which is why test 19 asserts on the error line."""
    payload = doc.ADULT_WORKOUT.replace("notes: pasted by hand", "notes: '<script>alert(1)</script>'")
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"suspicious_content"}


async def test_import_control_characters_rejected(seeded_client: httpx.AsyncClient) -> None:
    """A control character hidden in a JSON escape is decoded by the parser and caught after it."""
    payload = json.dumps(doc.as_dict(doc.ADULT_WORKOUT)).replace("My upper", "My\\u0007upper")
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"suspicious_content"}


async def test_import_deeply_nested_document(seeded_client: httpx.AsyncClient) -> None:
    """Test j: a nesting bomb is measured iteratively and refused before anything walks it."""
    payload = "id: deep\nname: Deep\nrows: " + "[" * 400 + "]" * 400 + "\n"
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 422
    assert "parse_error" in codes_of(response.json())


async def test_import_long_string_rejected(seeded_client: httpx.AsyncClient) -> None:
    payload = doc.ADULT_WORKOUT.replace("name: My upper", f"name: {'x' * 200}")
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 422
    assert "string_too_long" in codes_of(response.json())


# ---------------------------------------------------------------------------- exercises and ids


async def test_import_unknown_exercise_rejected(seeded_client: httpx.AsyncClient) -> None:
    """Test 14."""
    payload = doc.ADULT_WORKOUT.replace("exercise_id: push-up", "exercise_id: not-an-exercise")
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 422
    assert "unknown_exercise" in codes_of(response.json())


async def test_import_inline_exercise_accepted(seeded_client: httpx.AsyncClient, settings) -> None:
    """Test 15: an unknown id is allowed when the document defines it and it validates."""
    response = await seeded_client.post("/api/import", json={"text": doc.INLINE_EXERCISE_WORKOUT, "profile": "me"})
    assert response.status_code == 200, response.text
    assert stored(settings, "inline-day") is not None
    with DbSession(get_engine(settings)) as db:
        catalog = import_service.exercise_catalog(db)
    assert "towel-row" in catalog


async def test_inline_exercise_cannot_shadow_a_seed_one(seeded_client: httpx.AsyncClient) -> None:
    """Redefining ``push-up`` would let a document rewrite the facts every band check reads."""
    payload = doc.INLINE_EXERCISE_WORKOUT.replace("towel-row", "push-up")
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 422
    assert "id_collision" in codes_of(response.json())


async def test_import_unknown_top_level_keys_dropped(seeded_client: httpx.AsyncClient) -> None:
    """Test 16."""
    payload = f"author: someone\nversion: 3\n{doc.ADULT_WORKOUT}"
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 200, response.text
    assert set(response.json()["meta"]["ignored_keys"]) == {"author", "version"}


async def test_import_cannot_overwrite_seed(seeded_client: httpx.AsyncClient, settings) -> None:
    """Test 17: a seed id is refused, and the seed row is byte-identical afterwards."""
    before = stored(settings, "upper-a")
    assert before is not None and before.source == "seed"
    original = before.doc_json
    payload = doc.ADULT_WORKOUT.replace("id: my-upper", "id: upper-a")
    response = await seeded_client.post("/api/import", json={"text": payload, "profile": "me"})
    assert response.status_code == 422
    assert codes_of(response.json()) == {"id_collision"}
    after = stored(settings, "upper-a")
    assert after is not None
    assert after.doc_json == original
    assert after.source == "seed"


async def test_import_never_touches_filesystem(seeded_client: httpx.AsyncClient, monkeypatch) -> None:
    """Test 18: with every write path booby-trapped, the import still succeeds."""
    library_bundle()  # prime the process-wide bundle so the trap does not catch the library read.

    def refuse(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("the import path touched the filesystem")

    monkeypatch.setattr(Path, "write_text", refuse)
    monkeypatch.setattr(Path, "write_bytes", refuse)
    monkeypatch.setattr(Path, "open", refuse)
    monkeypatch.setattr(os, "makedirs", refuse)
    # Acceptance item 18 names ``builtins.open`` explicitly, and it was the one trap missing:
    # ``Path.open`` does not cover a bare ``open(path)``, which is the more obvious way to write
    # a file and the one a future change is likelier to reach for (D-196).
    monkeypatch.setattr("builtins.open", refuse)
    response = await seeded_client.post("/api/import", json={"text": doc.ADULT_WORKOUT, "profile": "me"})
    assert response.status_code == 200, response.text


# ------------------------------------------------------------------------------------- escaping


def test_autoescape_is_enabled() -> None:
    """Test 20: asserted on the environment, not assumed from the file extension."""
    assert templates.env.autoescape is True


def test_injection_payload_renders_inert() -> None:
    """Test 19, adapted: the refusal itself carries the payload back, so it must be escaped.

    ``<script`` is refused by the content scan, so no such cue can ever be stored and rendered on
    a checklist. What *is* rendered is the error line, which names the offending token from the
    fixed list and never the document - and that token is itself the literal ``<script``, so the
    panel is the one place markup from an import reaches HTML (D-175).
    """
    rendered = templates.get_template("partials/import_result.html").render(
        request=None,
        result={
            "ok": False,
            "errors": [{"code": "suspicious_content", "row": 2, "message": "<script>alert(1)</script>"}],
        },
        preview=None,
    )
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered


def test_a_stored_cue_renders_escaped() -> None:
    """Markup that is *not* on the block list still never reaches the browser as markup."""
    rendered = templates.get_template("partials/generate_preview.html").render(
        request=None,
        result=None,
        preview={
            "workout": type("W", (), {"name": "Trick"})(),
            "document": '{"id": "x"}',
            "profile": "me",
            "rows": [
                {
                    "name": "Push-up",
                    "cue": '" onmouseover=alert(1) x="',
                    "sets": 3,
                    "reps": 10,
                    "seconds": None,
                    "meters": None,
                    "steps": None,
                    "load_kg": None,
                }
            ],
            "day_types": (),
            "model": "code-plan",
            "attempts": 1,
        },
    )
    # The quotes are what matter: escaped, the payload is a line of text inside the cue element
    # and cannot close the attribute it would need to close to become an event handler.
    assert "&#34;" in rendered or "&quot;" in rendered
    assert '" onmouseover' not in rendered
    assert "<script" not in rendered


def test_the_seed_library_survives_the_content_scan(library) -> None:
    """Risk 14: the rule is proven compatible with every cue, name and note actually shipped."""
    for exercise in library.exercises.values():
        assert untrusted.scan_text(exercise.cue) == [], exercise.id
        assert untrusted.scan_text(exercise.name) == [], exercise.id
    for path in sorted(LIBRARY_PATH.rglob("*.yaml")):
        assert untrusted.scan_text(path.read_text(encoding="utf-8")) == [], path.name


def test_no_unsafe_yaml_loader_anywhere_in_cadence() -> None:
    """Risk 1: ``yaml.load(`` appears nowhere in the package, ``safe_load`` is the only parser."""
    package = Path(__file__).resolve().parents[1] / "cadence"
    for path in package.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "yaml.load(" not in text, path
        assert "yaml.unsafe_load" not in text, path
        assert "yaml.full_load" not in text, path
        assert "import pickle" not in text, path
        assert "eval(" not in text, path
