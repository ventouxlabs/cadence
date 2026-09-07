"""The five-minute loop: drain the write-back queue, then refresh the metrics cache.

The only background work in the app (``docs/architecture.md`` section 1). Three properties it has
to keep, each of which has cost someone a silent outage somewhere:

* **It sleeps first.** Refreshing at startup would race every test and every deploy smoke run,
  and overwrite a cache a fixture had just written. Five minutes from now is still every five
  minutes.
* **Every iteration is wrapped.** One exception must not end the loop, because a loop that dies
  stops syncing forever and says nothing (PRP-06 risk 9).
* **Mock mode still runs it.** The fake answers instead of the network, which is what makes
  PRP-09's smoke test exercise this path rather than skip it.

Each pass does three things in order: queue any finished session that somehow has no job
(``writeback.backfill``), drain what is due, then refresh the metrics cache.
"""

from __future__ import annotations

import asyncio
import logging

from sqlmodel import Session

from cadence.config import Settings
from cadence.db import get_engine
from cadence.vitalforge.client import VitalForgeClient
from cadence.vitalforge.metrics import refresh_all
from cadence.vitalforge.sync import drain
from cadence.vitalforge.writeback import backfill

logger = logging.getLogger(__name__)

INTERVAL_S = 300.0


async def run_once(settings: Settings) -> None:
    """One pass. Opens its own database session: the request-scoped one is long gone."""
    client = VitalForgeClient(settings)
    with Session(get_engine(settings)) as db:
        # Before draining, not after: a session queued by the backfill should be attempted on
        # this pass rather than waiting another five minutes.
        backfill(db, config=settings)
        report = await drain(db, client)
        if report.drained:
            logger.info("VitalForge queue drained: %s", report.as_dict())
        await refresh_all(db, client)


async def periodic_sync(settings: Settings, interval_s: float = INTERVAL_S) -> None:
    """Loop until cancelled. Cancellation propagates; nothing else does."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            await run_once(settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep looping; the next pass is a fresh chance
            logger.warning("the VitalForge periodic pass failed; retrying in %.0f s", interval_s, exc_info=True)
