"""stednavne: Danske Stednavne named places, one row per current name (incl. secondary aliases).
ports the retired registry._stednavn_records from a staging join.

ds_stednavn carries the name (skrivemaade, aktualitet-current) + a navngivetsted_objectid ref; the
30 ds geometry entities carry `geometry` keyed by objectid. each ds geom spec tags its named places
with a wire type_label. a stednavn objectid is the unique pk (guard a double-emit across geom
tables); the 4 absent geometry entities (rute/faergerutelinje/faergerutepunkt/ubearbejdetnavnlinje)
are tolerated via to_regclass. search-only; never a resolve target.
"""

from __future__ import annotations

import asyncpg
from bifrost.arms.normalize import normalize
from bifrost.db import STEDNAVNE_COLUMNS

from ..registers import ALL_ENTITIES, DS
from . import STAGING
from .lifecycle import ds_case
from .read import stream

_BATCH = 25_000

# the ds geometry entities the names join against, each tagging its places with a wire type
_DS_GEOMS = tuple(
    (s.table, s.type_label) for s in ALL_ENTITIES if s.register == DS and s.type_label
)

# aktualitet=historisk skrivemåder are distinct objectids served as retired rows (joined to the same
# place's geometry as their current sibling); lifecycle is classified from aktualitet.
_STEDNAVN_SQL = f"""
SELECT s.objectid AS stednavn_id, s.skrivemaade AS name, g.geometry AS geometry,
    {ds_case("s.aktualitet")} AS lifecycle
FROM "{{staging}}".ds_stednavn s
JOIN "{{staging}}".{{table}} g
    ON g.objectid = s.navngivetsted_objectid AND g._deleted IS NOT TRUE
WHERE s._deleted IS NOT TRUE AND s.skrivemaade IS NOT NULL AND g.geometry IS NOT NULL
"""


async def _flush(writer: asyncpg.Connection, batch: list[tuple]) -> int:
    n = len(batch)
    await writer.copy_records_to_table("stednavne", records=batch, columns=STEDNAVNE_COLUMNS)
    batch.clear()
    return n


async def load_stednavne(
    reader: asyncpg.Connection, writer: asyncpg.Connection, schema: str, *, staging: str = STAGING
) -> int:
    """derive gen.<schema>.stednavne by joining names to each present ds geometry entity; returns
    the deduped emitted count."""
    print("[i] streaming stednavne...")
    total = 0
    emitted: set[str] = set()  # stednavn objectid is the pk; first geometry entity to carry it wins
    batch: list[tuple] = []
    for table, type_label in _DS_GEOMS:
        if not await reader.fetchval("SELECT to_regclass($1)", f"{staging}.{table}"):
            print(f"[!] {table} absent; skipped")
            continue
        before = len(emitted)
        async for r in stream(reader, _STEDNAVN_SQL.format(staging=staging, table=table)):
            sid = r["stednavn_id"]
            if sid in emitted:
                continue
            emitted.add(sid)
            batch.append(
                (sid, r["name"], normalize(r["name"]), type_label, r["geometry"], r["lifecycle"])
            )
            if len(batch) >= _BATCH:
                total += await _flush(writer, batch)
        print(f"[i] stednavne {table}: {len(emitted) - before} names")
    if batch:
        total += await _flush(writer, batch)
    print(f"[+] copied {total} stednavne")
    return total
