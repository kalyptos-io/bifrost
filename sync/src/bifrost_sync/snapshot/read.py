"""asyncpg server-side cursor streaming: iterate a large result without materializing it.

the cursor pins a transaction for its whole lifetime, so its connection is dedicated while
iterating - run COPY/DDL on a *second* connection meanwhile (the snapshot's reader/writer split).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg

DEFAULT_PREFETCH = 10_000


async def stream(
    conn: asyncpg.Connection, sql: str, *args: object, prefetch: int = DEFAULT_PREFETCH
) -> AsyncIterator[asyncpg.Record]:
    """yield rows of `sql` through a server-side cursor (fetched `prefetch` at a time)."""
    async with conn.transaction():
        async for record in conn.cursor(sql, *args, prefetch=prefetch):
            yield record


async def has_table(conn: asyncpg.Connection, qualified: str) -> bool:
    """True if the schema-qualified relation resolves (a staged table may be absent this gen)."""
    return bool(await conn.fetchval("SELECT to_regclass($1)", qualified))
