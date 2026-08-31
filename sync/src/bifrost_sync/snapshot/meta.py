"""sync watermark: the per-entity cursor + contract vector the newest generation was derived from.

lives in the staging schema (key/jsonb), stamped after a successful register(). the delta path
compares live pipeline cursors/contracts against it to decide whether a fresh snapshot is even
needed - both a cursor move and a contract change trigger one.
"""

from __future__ import annotations

import json

import asyncpg

from . import STAGING

SYNC_META_DDL = """
CREATE SCHEMA IF NOT EXISTS "{staging}";
CREATE TABLE IF NOT EXISTS "{staging}".sync_meta (
    key        text        PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

_WATERMARK = "watermark"


async def stamp_watermark(
    conn: asyncpg.Connection,
    cursors: dict[str, int],
    contracts: dict[str, str],
    *,
    staging: str = STAGING,
) -> None:
    await conn.execute(SYNC_META_DDL.format(staging=staging))
    value = {t: {"gen": g, "contract": contracts.get(t)} for t, g in cursors.items()}
    await conn.execute(
        f'INSERT INTO "{staging}".sync_meta (key, value, updated_at) '
        "VALUES ($1, $2::jsonb, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        _WATERMARK,
        json.dumps(value, sort_keys=True),
    )


async def read_watermark(conn: asyncpg.Connection, *, staging: str = STAGING) -> dict[str, object]:
    if not await conn.fetchval("SELECT to_regclass($1)", f"{staging}.sync_meta"):
        return {}
    value = await conn.fetchval(
        f'SELECT value FROM "{staging}".sync_meta WHERE key = $1', _WATERMARK
    )
    return json.loads(value) if value else {}
