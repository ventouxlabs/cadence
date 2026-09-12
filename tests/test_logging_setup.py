"""D-286: the package's own logs have to reach production, and be attributable when they do.

The bug these guard was invisible by construction — a dropped log record leaves no evidence that
it was dropped. It was found only by probing the running container, where `cadence.db`'s
migration line turned out to be going nowhere. So these assert on what actually comes out of a
handler, not on the configuration that is supposed to produce it.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from cadence.config import Settings
from cadence.logging_setup import ROOT_NAME, configure_logging


@pytest.fixture(autouse=True)
def _restore_logger() -> Iterator[None]:
    """Leave the `cadence` logger as it was; these tests mutate process-global state."""
    logger = logging.getLogger(ROOT_NAME)
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    yield
    logger.handlers = handlers
    logger.setLevel(level)
    logger.propagate = propagate


def test_an_info_record_actually_comes_out() -> None:
    """The whole bug. In production `logger.info` wrote to nothing at all."""
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("cadence.db").info("added challenge.met_on to an existing database")

    out = stream.getvalue()
    assert "added challenge.met_on" in out, f"an INFO record was dropped: {out!r}"


def test_the_line_says_which_logger_wrote_it() -> None:
    """`lastResort` printed bare message text, so a sync warning and a validator warning read alike."""
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("cadence.vitalforge.sync").warning("no person slug for profile %r", "son")

    out = stream.getvalue()
    assert "cadence.vitalforge.sync" in out, f"the record is unattributable: {out!r}"
    assert "WARNING" in out, f"the record does not say its level: {out!r}"
    assert "no person slug for profile 'son'" in out, out


def test_a_child_logger_anywhere_in_the_package_is_covered() -> None:
    """Configured on the namespace, not per module — a new module must not have to opt in."""
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)
    logging.getLogger("cadence.some.module.written.tomorrow").info("hello")
    assert "hello" in stream.getvalue()


def test_reconfiguring_does_not_stack_handlers() -> None:
    """The suite builds many apps in one process; stacking would duplicate a line per app."""
    first = io.StringIO()
    configure_logging("INFO", stream=first)
    second = io.StringIO()
    logger = configure_logging("INFO", stream=second)

    assert len([h for h in logger.handlers if getattr(h, "name", None) == "cadence-stream"]) == 1
    logging.getLogger("cadence.db").info("once")
    assert second.getvalue().count("once") == 1
    assert "once" not in first.getvalue(), "the old handler is still attached"


def test_the_level_is_honoured_in_both_directions() -> None:
    """A knob that only ever widens is not a knob."""
    quiet = io.StringIO()
    configure_logging("WARNING", stream=quiet)
    logging.getLogger("cadence.db").info("should not appear")
    logging.getLogger("cadence.db").warning("should appear")
    assert "should not appear" not in quiet.getvalue()
    assert "should appear" in quiet.getvalue()

    loud = io.StringIO()
    configure_logging("DEBUG", stream=loud)
    logging.getLogger("cadence.db").debug("debug line")
    assert "debug line" in loud.getvalue()


def test_records_still_reach_a_root_handler() -> None:
    """Propagation stays on, and this is the test that says why.

    The first version set `propagate = False` to avoid double-printing. Production's root logger
    has no handlers, so there was nothing to double — and switching it off silently broke every
    caller that captures through the root, `caplog` included. Three unrelated writeback tests
    failed depending on whether an app had been built earlier in the same process.
    """
    stream = io.StringIO()
    configure_logging("INFO", stream=stream)

    root_capture = io.StringIO()
    root_handler = logging.StreamHandler(root_capture)
    root = logging.getLogger()
    root.addHandler(root_handler)
    try:
        logging.getLogger("cadence.db").info("reaches both")
    finally:
        root.removeHandler(root_handler)

    assert "reaches both" in stream.getvalue(), "the package handler lost the record"
    assert "reaches both" in root_capture.getvalue(), (
        "propagation is off again - caplog and any operator-added root handler go blind"
    )


def test_caplog_still_sees_package_records(caplog: pytest.LogCaptureFixture) -> None:
    """The regression itself, as pytest meets it."""
    configure_logging("INFO")
    with caplog.at_level(logging.INFO, logger="cadence.db"):
        logging.getLogger("cadence.db").info("visible to caplog")
    assert "visible to caplog" in caplog.text


def test_an_unknown_level_name_falls_back_rather_than_raising() -> None:
    """Startup must not die on a typo in `.env`. A wrong level is not worth a down app."""
    stream = io.StringIO()
    configure_logging("NOISY", stream=stream)
    logging.getLogger("cadence.db").info("still here")
    assert "still here" in stream.getvalue()


def test_the_setting_defaults_to_info_and_is_an_env_alias() -> None:
    """The knob exists and defaults to the level that was missing."""
    assert Settings().log_level == "INFO"
    alias = Settings.model_fields["log_level"].validation_alias
    assert alias == "CADENCE_LOG_LEVEL"


def test_creating_the_app_actually_configures_logging() -> None:
    """The wiring, not the unit. Removing the `configure_logging` call from `create_app`'s
    lifespan passed every test above — the same shape as D-283, where `has_column` worked
    perfectly and the path that mattered was never exercised.

    Asserted through the lifespan, because that is where it runs: the handler does not exist
    until the app starts.
    """
    from fastapi.testclient import TestClient

    from cadence.main import create_app

    logger = logging.getLogger(ROOT_NAME)
    for existing in list(logger.handlers):
        if getattr(existing, "name", None) == "cadence-stream":
            logger.removeHandler(existing)
    assert not [h for h in logger.handlers if getattr(h, "name", None) == "cadence-stream"]

    with TestClient(create_app()):
        attached = [h for h in logging.getLogger(ROOT_NAME).handlers if getattr(h, "name", None) == "cadence-stream"]
    assert attached, "starting the app left the package's logs going nowhere (D-286)"


def test_the_app_configures_logging_before_the_database_migrates() -> None:
    """Order matters: `init_db` may log a column migration (D-283), and a line written before
    the handler exists is lost exactly like the bug this fixes.
    """
    source = (Path(__file__).parent.parent / "cadence" / "main.py").read_text()
    assert "configure_logging" in source, "the app no longer configures logging at all"
    assert source.index("configure_logging(") < source.index("init_db("), (
        "init_db runs before logging is configured, so its migration line goes nowhere"
    )
