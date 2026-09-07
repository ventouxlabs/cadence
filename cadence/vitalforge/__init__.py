"""The VitalForge integration: read the metrics, write the session back.

``client`` talks HTTP and nothing else; ``metrics`` turns reads into the cache every screen uses;
``payload`` builds the exact ``ActivityIn`` body; ``sync`` owns the bounded retry queue;
``writeback`` is what Done calls; ``periodic`` is the five-minute loop. Nothing here raises an
``httpx`` exception at a caller, and nothing here ever puts the token in a log line.

Deliberately re-exports nothing. ``cadence/db.py`` imports ``vitalforge.tables`` so
``create_all`` can see the two tables, and ``sync`` imports ``cadence.db`` for ``update_model``:
a package ``__init__`` that pulled the submodules in eagerly would close that loop. Import the
module you want.
"""
