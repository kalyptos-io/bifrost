"""pg session advisory lock serializing every sync run against one database.

a dedicated psycopg2 connection holds pg_try_advisory_lock for the run's duration; closing it
releases the lock. it spans the whole synchronous reconcile (dlt staging + the async snapshot
island), which an asyncpg hold could not - that connection is bound to one event loop. one-shot
commands turn a held lock into a loud failure; the worker treats it as transient (backoff).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import psycopg2

# sha256("bifrost-sync")[:8] as a signed bigint - stable cross-process key, one lock per database
LOCK_KEY = 8182085673014418664


class LockHeld(RuntimeError):
    """another sync run already holds the advisory lock."""


@contextlib.contextmanager
def advisory_lock(dsn: str, *, key: int = LOCK_KEY) -> Iterator[None]:
    # keepalives: idle multi-hour load must not let an idle-drop silently release the lock
    conn = psycopg2.connect(
        dsn, keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            if not cur.fetchone()[0]:
                raise LockHeld("another sync run is active")
        yield
    finally:
        conn.close()  # session lock released on close
