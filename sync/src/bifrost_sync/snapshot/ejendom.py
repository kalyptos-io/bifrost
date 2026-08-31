"""ejendom: one row per bfe across the three property types (sfe, ejerlejlighed, bpfg), so any bfe
is a single pk probe. derived on the writer by INSERT ... SELECT over the staging mat joins - the
_SP_INSERT precedent. runs AFTER load_matrikel (representative parcel + multi-parcel geometry come
off gen.matrikel) and BEFORE load_addresses (the ejendom_bfe stamp joins the populated table).

chain is self -> ground (incl. self, depth <= 3); a unit's parent precedence is bpfg-first (punkt
over flade over sfe), a bpfg parent lifting ground to the bpfg's own sfe. children invert parent_bfe
in one pass; geometry is the pre-merged ground footprint filled only for multi-parcel sfe.
"""

from __future__ import annotations

from typing import NamedTuple

import asyncpg

from . import STAGING
from .lifecycle import mat_case
from .matrikel import merge_polygons
from .read import stream

_GEOM_BATCH = 25_000  # rows carry a merged geometry; matches the matrikel batch


class EjendomCounts(NamedTuple):
    sfe: int
    ejerlejlighed: int
    bpfg: int

    @property
    def total(self) -> int:
        return self.sfe + self.ejerlejlighed + self.bpfg


# sfe: self-chain, ground is self. lifecycle classified from the sfe status (Historisk -> retired)
_INSERT_SFE = f"""
INSERT INTO ejendom (bfe, type, parent_bfe, ground_bfe, chain_bfes, chain_types, lifecycle)
SELECT DISTINCT ON (bfe)
    bfe, 'samlet_fast_ejendom', NULL, bfe, ARRAY[bfe], ARRAY['samlet_fast_ejendom'],
    {mat_case("status", "virkningfra", "virkningtil")}
FROM "{{staging}}".mat_samletfastejendom
WHERE _deleted IS NOT TRUE AND bfe IS NOT NULL
ORDER BY bfe
"""

# bpfg: punkt wins over flade (pri); ground sfe nullable -> chain truncates to self
_INSERT_BPFG = f"""
INSERT INTO ejendom (bfe, type, parent_bfe, ground_bfe, chain_bfes, chain_types, lifecycle)
SELECT DISTINCT ON (b.bfe)
    b.bfe,
    'bygning_paa_fremmed_grund',
    sfe.bfe,
    sfe.bfe,
    CASE WHEN sfe.bfe IS NOT NULL THEN ARRAY[b.bfe, sfe.bfe] ELSE ARRAY[b.bfe] END,
    CASE WHEN sfe.bfe IS NOT NULL
         THEN ARRAY['bygning_paa_fremmed_grund', 'samlet_fast_ejendom']
         ELSE ARRAY['bygning_paa_fremmed_grund'] END,
    {mat_case("b.status", "b.virkningfra", "b.virkningtil")}
FROM (
    SELECT bfe, sfe_lokalid, status, virkningfra, virkningtil, 0 AS pri
    FROM "{{staging}}".mat_bygningpaafremmedgrundpunkt
    WHERE _deleted IS NOT TRUE AND bfe IS NOT NULL
    UNION ALL
    SELECT bfe, sfe_lokalid, status, virkningfra, virkningtil, 1 AS pri
    FROM "{{staging}}".mat_bygningpaafremmedgrundflade
    WHERE _deleted IS NOT TRUE AND bfe IS NOT NULL
) b
LEFT JOIN "{{staging}}".mat_samletfastejendom sfe
    ON sfe.id = b.sfe_lokalid AND sfe._deleted IS NOT TRUE AND sfe.bfe IS NOT NULL
ORDER BY b.bfe, b.pri
"""

# ejerlejlighed: parent precedence bpfg-punkt > bpfg-flade > sfe; a bpfg parent lifts ground to the
# bpfg's own sfe (depth-3 chain), else the direct sfe is the ground (depth-2)
_INSERT_EJERLEJLIGHED = f"""
INSERT INTO ejendom
    (bfe, type, ejerlejlighedsnummer, parent_bfe, ground_bfe, chain_bfes, chain_types, lifecycle)
SELECT DISTINCT ON (u.bfe)
    u.bfe,
    'ejerlejlighed',
    u.ejerlejlighedsnummer,
    COALESCE(u.bpfg_bfe, u.sfe_direct_bfe),
    CASE WHEN u.bpfg_bfe IS NOT NULL THEN gp.bfe
         WHEN u.sfe_direct_bfe IS NOT NULL THEN u.sfe_direct_bfe END,
    CASE
        WHEN u.bpfg_bfe IS NOT NULL AND gp.bfe IS NOT NULL THEN ARRAY[u.bfe, u.bpfg_bfe, gp.bfe]
        WHEN u.bpfg_bfe IS NOT NULL THEN ARRAY[u.bfe, u.bpfg_bfe]
        WHEN u.sfe_direct_bfe IS NOT NULL THEN ARRAY[u.bfe, u.sfe_direct_bfe]
        ELSE ARRAY[u.bfe] END,
    CASE
        WHEN u.bpfg_bfe IS NOT NULL AND gp.bfe IS NOT NULL
            THEN ARRAY['ejerlejlighed', 'bygning_paa_fremmed_grund', 'samlet_fast_ejendom']
        WHEN u.bpfg_bfe IS NOT NULL
            THEN ARRAY['ejerlejlighed', 'bygning_paa_fremmed_grund']
        WHEN u.sfe_direct_bfe IS NOT NULL
            THEN ARRAY['ejerlejlighed', 'samlet_fast_ejendom']
        ELSE ARRAY['ejerlejlighed'] END,
    {mat_case("u.status", "u.virkningfra", "u.virkningtil")}
FROM (
    SELECT
        el.bfe AS bfe,
        el.ejerlejlighedsnummer AS ejerlejlighedsnummer,
        el.status AS status,
        el.virkningfra AS virkningfra,
        el.virkningtil AS virkningtil,
        COALESCE(bp.bfe, bf.bfe) AS bpfg_bfe,
        CASE WHEN bp.bfe IS NOT NULL THEN bp.sfe_lokalid
             WHEN bf.bfe IS NOT NULL THEN bf.sfe_lokalid END AS bpfg_sfe_lokalid,
        sfe.bfe AS sfe_direct_bfe
    FROM "{{staging}}".mat_ejerlejlighed el
    LEFT JOIN "{{staging}}".mat_bygningpaafremmedgrundpunkt bp
        ON bp.id = el.bpfg_punkt_lokalid AND bp._deleted IS NOT TRUE AND bp.bfe IS NOT NULL
    LEFT JOIN "{{staging}}".mat_bygningpaafremmedgrundflade bf
        ON bf.id = el.bpfg_flade_lokalid AND bf._deleted IS NOT TRUE AND bf.bfe IS NOT NULL
    LEFT JOIN "{{staging}}".mat_samletfastejendom sfe
        ON sfe.id = el.sfe_lokalid AND sfe._deleted IS NOT TRUE AND sfe.bfe IS NOT NULL
    WHERE el._deleted IS NOT TRUE AND el.bfe IS NOT NULL
) u
LEFT JOIN "{{staging}}".mat_samletfastejendom gp
    ON gp.id = u.bpfg_sfe_lokalid AND gp._deleted IS NOT TRUE AND gp.bfe IS NOT NULL
ORDER BY u.bfe
"""

# units referencing a bpfg through both the punkt and flade pointer; punkt wins, this counts them
_DUAL_BPFG_REFS = """
SELECT count(*)
FROM "{staging}".mat_ejerlejlighed el
JOIN "{staging}".mat_bygningpaafremmedgrundpunkt bp
    ON bp.id = el.bpfg_punkt_lokalid AND bp._deleted IS NOT TRUE AND bp.bfe IS NOT NULL
JOIN "{staging}".mat_bygningpaafremmedgrundflade bf
    ON bf.id = el.bpfg_flade_lokalid AND bf._deleted IS NOT TRUE AND bf.bfe IS NOT NULL
WHERE el._deleted IS NOT TRUE AND el.bfe IS NOT NULL
"""

_CHILDREN = """
UPDATE ejendom c SET children_bfes = a.bfes, children_types = a.types
FROM (
    SELECT parent_bfe,
           array_agg(bfe ORDER BY bfe) AS bfes,
           array_agg(type ORDER BY bfe) AS types
    FROM ejendom WHERE parent_bfe IS NOT NULL GROUP BY parent_bfe
) a
WHERE c.bfe = a.parent_bfe
"""

_REPRESENTATIVE_PARCEL = """
UPDATE ejendom e SET jordstykke = a.j
FROM (SELECT bfe, min(jordstykke) AS j FROM matrikel GROUP BY bfe) a
WHERE e.bfe = a.bfe
"""

_ORPHANED_PARENTS = """
SELECT count(*) FROM ejendom c
WHERE c.parent_bfe IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM ejendom p WHERE p.bfe = c.parent_bfe)
"""

_TYPE_COUNTS = "SELECT type, count(*) AS n FROM ejendom GROUP BY type"
_TRUNCATED = "SELECT count(*) FROM ejendom WHERE ground_bfe IS NULL"

# multi-parcel sfe geometry is folded in python (the merge); everything else is set-based sql
_MULTI_PARCEL = (
    'SELECT bfe, array_agg(geometry) AS geoms FROM "{schema}".matrikel '
    "GROUP BY bfe HAVING count(*) > 1"
)
_MERGE_UPDATE = (
    "UPDATE ejendom e SET geometry = u.geometry "
    "FROM unnest($1::text[], $2::text[]) AS u(bfe, geometry) WHERE e.bfe = u.bfe"
)


async def _flush_geometry(writer: asyncpg.Connection, batch: list[tuple[str, str]]) -> None:
    await writer.execute(_MERGE_UPDATE, [b for b, _ in batch], [g for _, g in batch])


async def _merge_geometry(
    reader: asyncpg.Connection, writer: asyncpg.Connection, schema: str
) -> None:
    batch: list[tuple[str, str]] = []
    async for r in stream(reader, _MULTI_PARCEL.format(schema=schema)):
        merged = merge_polygons(r["geoms"])
        if merged is None:
            continue
        batch.append((r["bfe"], merged))
        if len(batch) >= _GEOM_BATCH:
            await _flush_geometry(writer, batch)
            batch.clear()
    if batch:
        await _flush_geometry(writer, batch)


async def load_ejendom(
    reader: asyncpg.Connection, writer: asyncpg.Connection, schema: str, *, staging: str = STAGING
) -> EjendomCounts:
    """derive gen.<schema>.ejendom from staging + gen.matrikel; returns the per-type row counts."""
    print("[i] deriving ejendom...")
    await writer.execute(_INSERT_SFE.format(staging=staging))
    await writer.execute(_INSERT_BPFG.format(staging=staging))
    dual = await writer.fetchval(_DUAL_BPFG_REFS.format(staging=staging))
    await writer.execute(_INSERT_EJERLEJLIGHED.format(staging=staging))
    await writer.execute(_CHILDREN)
    await writer.execute(_REPRESENTATIVE_PARCEL)
    await _merge_geometry(reader, writer, schema)

    counts = {r["type"]: r["n"] for r in await writer.fetch(_TYPE_COUNTS)}
    out = EjendomCounts(
        counts.get("samlet_fast_ejendom", 0),
        counts.get("ejerlejlighed", 0),
        counts.get("bygning_paa_fremmed_grund", 0),
    )
    orphaned = await writer.fetchval(_ORPHANED_PARENTS)
    truncated = await writer.fetchval(_TRUNCATED)
    if dual:
        print(f"[!] {dual} ejerlejligheder reference both a bpfg punkt and flade (punkt wins)")
    if orphaned:
        print(f"[!] {orphaned} ejendom rows point at a missing parent bfe")
    if truncated:
        print(f"[!] {truncated} ejendom rows have no ground sfe (chain truncated)")
    print(
        f"[+] derived {out.total} ejendom "
        f"(sfe {out.sfe}, ejerlejlighed {out.ejerlejlighed}, bpfg {out.bpfg})"
    )
    return out
