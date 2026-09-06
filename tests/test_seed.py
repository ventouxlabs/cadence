"""``make seed`` must exit 0 before PRP-01 lands, and say why."""

from __future__ import annotations

from pathlib import Path

import pytest

from cadence.bibliotheque import seed


def test_seed_exits_zero_with_no_library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(seed, "LIBRARY_DIR", tmp_path)
    assert seed.main() == 0
    assert "no library yet" in capsys.readouterr().out


def test_seed_notices_library_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys) -> None:
    (tmp_path / "exercises").mkdir()
    (tmp_path / "exercises" / "push-up.yaml").write_text("id: push-up\n")
    monkeypatch.setattr(seed, "LIBRARY_DIR", tmp_path)
    assert seed.main() == 0
    assert "exercises" in capsys.readouterr().out
