"""road: one row per physical road (navngivenvej) with >=1 current address. ports the retired
registry._road_records / load._road_record from a staging join.

geometry is the road's own complete (Multi)LineString (no per-postcode split); postcodes[] are the
postcodes its addresses touch (disambiguates same-named roads + confines a pinned query). the
vejnavn folds to a street_id via the address-stream StreetIds - a road whose street never appeared
in the address fact is an orphan, dropped (StreetIds.lookup mints nothing).
"""

from __future__ import annotations

import asyncpg
from bifrost.arms.normalize import normalize
from bifrost.db import ROAD_COLUMNS

from . import STAGING
from .lifecycle import dar_case
from .read import stream
from .records import StreetIds

_BATCH = 25_000

# postcodes aggregated per road via husnummer (small key), then joined back to nv for geometry so a
# huge linestring is never a GROUP BY key. a road needs >=1 husnummer with a live postcode; geometry
# is nullable now (a retired road may lack it). lifecycle is from the navngivenvej status.
_ROAD_SQL = f"""
SELECT nv.id AS navngivenvej_id, nv.vejnavn AS vejnavn, nv.geometry AS geometry, p.postcodes,
    {dar_case("nv.status", "nv.virkningfra", "nv.virkningtil")} AS lifecycle
FROM "{{staging}}".dar_navngivenvej nv
JOIN (
    SELECT h.navngivenvej AS nvid, array_agg(DISTINCT pn.postnr) AS postcodes
    FROM "{{staging}}".dar_husnummer h
    JOIN "{{staging}}".dar_postnummer pn ON pn.id = h.postnummer AND pn._deleted IS NOT TRUE
    WHERE h._deleted IS NOT TRUE AND h.navngivenvej IS NOT NULL
    GROUP BY h.navngivenvej
) p ON p.nvid = nv.id
WHERE nv._deleted IS NOT TRUE AND nv.vejnavn IS NOT NULL
"""


def road_tuple(r: asyncpg.Record, ids: StreetIds) -> tuple | None:
    """one road COPY tuple; folds vejnavn -> street_id, drops orphans (street with no address)."""
    sid = ids.lookup(normalize(r["vejnavn"]))
    if sid is None:
        return None
    return (r["navngivenvej_id"], sid, sorted(r["postcodes"]), r["geometry"], r["lifecycle"])


async def load_roads(
    reader: asyncpg.Connection,
    writer: asyncpg.Connection,
    schema: str,
    ids: StreetIds,
    *,
    staging: str = STAGING,
) -> int:
    """derive gen.<schema>.road from the staging navngivenvej/husnummer/postnummer join; returns the
    emitted road count. needs the fully-accrued StreetIds, so it runs after the address stream."""
    print("[i] streaming roads...")
    total = orphans = 0
    batch: list[tuple] = []
    async for r in stream(reader, _ROAD_SQL.format(staging=staging)):
        rec = road_tuple(r, ids)
        if rec is None:
            orphans += 1
            continue
        batch.append(rec)
        if len(batch) >= _BATCH:
            await writer.copy_records_to_table("road", records=batch, columns=ROAD_COLUMNS)
            total += len(batch)
            batch.clear()
    if batch:
        await writer.copy_records_to_table("road", records=batch, columns=ROAD_COLUMNS)
        total += len(batch)
    print(f"[+] copied {total} roads ({orphans} orphans dropped)")
    return total
