"""The response envelope - acceptance test 28."""

from __future__ import annotations

from cadence.api.envelope import ENVELOPE_KEYS, err, ok


def test_envelope_shape() -> None:
    success = ok({"a": 1}, page=2)
    failure = err("nope", data=None, retry_in=5)
    assert set(success) == ENVELOPE_KEYS == set(failure)
    assert success == {"ok": True, "data": {"a": 1}, "error": None, "meta": {"page": 2}}
    assert failure == {"ok": False, "data": None, "error": "nope", "meta": {"retry_in": 5}}
