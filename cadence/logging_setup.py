"""One handler on the ``cadence`` namespace, because production had none (D-286).

Nothing in this package configured logging, and `uvicorn`'s CLI configures only its own
`uvicorn*` loggers. So every `cadence.*` record propagated to a root logger with no handlers and
landed on `logging.lastResort` — which is **WARNING level and unformatted**. Probed on VM-201
inside the running container:

    root handlers      : []
    root level         : WARNING
    cadence.db handlers: []   propagate: True
    lastResort level   : WARNING

Two consequences, both silent. Every ``logger.info`` in the package was dropped, including the
line `cadence.db._add_missing_columns` writes when it migrates a column onto a live database
(D-283) — a schema change with no trace in the logs. And the warnings that did survive arrived
as bare message text with no timestamp, level or logger name, interleaved with uvicorn's
formatted lines and impossible to attribute.

Deliberately scoped to the ``cadence`` namespace rather than the root logger: uvicorn owns its
own output and `basicConfig` on the root would either fight it or duplicate every access line.

**Propagation is left on.** The first version turned it off, reasoning that a record handled
here must not also reach a handler above — but the probe that found this bug also showed the
root logger has *no* handlers under uvicorn, so there is nothing above to reach, and
`logging.lastResort` never fires once any handler has taken the record. What switching it off
did break was every caller that captures through the root: pytest's ``caplog`` works that way,
and three unrelated writeback tests started failing depending on whether an app had been built
earlier in the process. Guarding a case that does not exist, at the cost of one that does.
If an operator later attaches a root handler — a file, a JSON shipper — they want these records
in it.
"""

from __future__ import annotations

import logging
import sys

#: The one logger this touches. Everything in the package is a child of it.
ROOT_NAME = "cadence"

#: Timestamp, level, logger, message. The logger name is what makes a warning attributable —
#: `lastResort` omitted it, which is how a sync warning and a validator warning read alike.
FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

#: Marks the handler as ours so a second call can find it. Reconfiguring is a real case — the
#: test suite builds many apps in one process — and stacking handlers would duplicate a line per
#: app ever created.
_HANDLER_NAME = "cadence-stream"


def configure_logging(level: str | int = logging.INFO, *, stream: object | None = None) -> logging.Logger:
    """Attach (or re-point) the package's single stream handler. Idempotent.

    Returns the configured logger so a caller can assert on it. ``stream`` is for tests; it
    defaults to stderr, where uvicorn also writes, so container logs stay one ordered stream.
    """
    logger = logging.getLogger(ROOT_NAME)
    resolved = logging.getLevelNamesMapping().get(level.upper(), logging.INFO) if isinstance(level, str) else level

    for existing in list(logger.handlers):
        if getattr(existing, "name", None) == _HANDLER_NAME:
            logger.removeHandler(existing)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)  # type: ignore[arg-type]
    handler.name = _HANDLER_NAME
    handler.setFormatter(logging.Formatter(FORMAT, datefmt=DATE_FORMAT))
    handler.setLevel(resolved)
    logger.addHandler(handler)
    logger.setLevel(resolved)
    # Left on deliberately; see the module docstring. Set explicitly rather than assumed, because
    # a previous call in the same process may have turned it off.
    logger.propagate = True
    return logger
