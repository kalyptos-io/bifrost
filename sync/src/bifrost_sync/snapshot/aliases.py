"""name-history aliases: a historical designation -> the canonical serving id, searchable via the
existing in-proc indexes (street/area).

- street_alias: a road's prior vejnavn (from dar_navngivenvej_hist, differing from the current name)
  -> the current road's street_id + its postcodes (scoped, no same-name fan-out). the road join
  drops orphans (a rename whose road never made the fact).
- area_alias: a postdistrikt's prior navn (from dar_postnummer_hist) -> its postcode admin_area.

runs after load_roads/load_areas (needs the derived gen tables). all tiny. (retired parcel
betegnelser need no alias: matrikel keeps its non-current rows for the history betegnelse KNN.)
"""

from __future__ import annotations

import asyncpg
from bifrost.arms.normalize import normalize
from bifrost.db import AREA_ALIAS_COLUMNS, STREET_ALIAS_COLUMNS

from . import STAGING
from .lifecycle import RETIRED
from .read import has_table

# a prior name (differs from the current) -> the current road's street_id + postcodes
_STREET_ALIAS_SQL = """
SELECT DISTINCT h.vejnavn AS name, rd.street_id AS street_id, rd.postcodes AS postcodes
FROM "{staging}".dar_navngivenvej_hist h
JOIN "{staging}".dar_navngivenvej nv ON nv.id = h.id AND nv._deleted IS NOT TRUE
JOIN "{schema}".road rd ON rd.navngivenvej_id = h.id
WHERE h.vejnavn IS NOT NULL AND h.vejnavn <> nv.vejnavn
"""

# a prior postdistrikt name -> its postcode admin_area (by postnr = code)
_AREA_ALIAS_SQL = """
SELECT DISTINCT h.navn AS name, aa.area_id AS area_id
FROM "{staging}".dar_postnummer_hist h
JOIN "{staging}".dar_postnummer pn ON pn.id = h.id AND pn._deleted IS NOT TRUE
JOIN "{schema}".admin_area aa ON aa.kind = 'postcode' AND aa.code = pn.postnr
WHERE h.navn IS NOT NULL AND h.navn <> pn.navn
"""


async def load_aliases(
    reader: asyncpg.Connection, writer: asyncpg.Connection, schema: str, *, staging: str = STAGING
) -> tuple[int, int]:
    """derive the two alias tables; returns (street, area) counts."""
    print("[i] deriving name aliases...")
    street = area = 0

    if await has_table(reader, f"{staging}.dar_navngivenvej_hist"):
        rows = await reader.fetch(_STREET_ALIAS_SQL.format(staging=staging, schema=schema))
        batch = [
            (r["name"], normalize(r["name"]), r["street_id"], sorted(r["postcodes"]), RETIRED)
            for r in rows
        ]
        if batch:
            await writer.copy_records_to_table(
                "street_alias", records=batch, columns=STREET_ALIAS_COLUMNS
            )
            street = len(batch)

    if await has_table(reader, f"{staging}.dar_postnummer_hist"):
        rows = await reader.fetch(_AREA_ALIAS_SQL.format(staging=staging, schema=schema))
        batch = [(r["area_id"], r["name"], normalize(r["name"]), RETIRED) for r in rows]
        if batch:
            await writer.copy_records_to_table(
                "area_alias", records=batch, columns=AREA_ALIAS_COLUMNS
            )
            area = len(batch)

    print(f"[+] aliases: {street} street, {area} area")
    return street, area
