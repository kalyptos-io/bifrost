"""matrikel: one row per current, geometry-carrying jordstykke (parcel). ports the retired
registry.generate_matrikel from a national staging join instead of per-muni csv files.

jordstykke JOINs its samletfastejendom (bfe, NOT NULL guard), ejerlav (kode + navn; the sniffed
ejerlavskode staging column, all-digit-id fallback), centroide ("x y", keyed by the parcel), and its
lodflader (many polygons -> one merged (Multi)Polygon). kommunekode is the parcel's own staging
column; kommunenavn is resolved from dagi by code. runs BEFORE the address stream: an address keeps
its jordstykke ref only where gen.matrikel already holds the parcel.
"""

from __future__ import annotations

import json

import asyncpg
from bifrost.arms.normalize import normalize
from bifrost.db import MATRIKEL_COLUMNS

from . import STAGING
from .lifecycle import CURRENT, mat_case
from .read import stream

_BATCH = 25_000  # parcels carry a merged geometry; smaller than the thin-address batch

# lodflader hash-aggregated per parcel (no global sort) into a text[] the python merge folds; the
# ejerlavskode falls back to an all-digit id_lokalId when the sniff found no ejerlav*kode column.
# lifecycle is classified from the jordstykke status (Historisk = retired; kept for history search).
_MATRIKEL_SQL = f"""
WITH lf AS (
    SELECT jordstykke, array_agg(geometry) AS geoms
    FROM "{{staging}}".mat_lodflade
    WHERE _deleted IS NOT TRUE AND geometry IS NOT NULL AND jordstykke IS NOT NULL
    GROUP BY jordstykke
)
SELECT
    j.id                                                                     AS jordstykke,
    sfe.bfe                                                                   AS bfe,
    j.matrikelnummer                                                         AS matrikelnummer,
    COALESCE(el.ejerlavskode, CASE WHEN el.id ~ '^[0-9]+$' THEN el.id END)   AS ejerlavskode,
    el.ejerlavsnavn                                                          AS ejerlavsnavn,
    j.kommunekode                                                            AS kommunekode,
    c.centroid                                                               AS centroid,
    lf.geoms                                                                 AS lodflade_geoms,
    {mat_case("j.status", "j.virkningfra", "j.virkningtil")}                 AS lifecycle
FROM "{{staging}}".mat_jordstykke j
LEFT JOIN "{{staging}}".mat_samletfastejendom sfe
    ON sfe.id = j.samletfastejendom_lokalid AND sfe._deleted IS NOT TRUE
LEFT JOIN "{{staging}}".mat_ejerlav el
    ON el.id = j.ejerlav_lokalid AND el._deleted IS NOT TRUE
LEFT JOIN "{{staging}}".mat_centroide c
    ON c.jordstykke = j.id AND c._deleted IS NOT TRUE
LEFT JOIN lf ON lf.jordstykke = j.id
WHERE j._deleted IS NOT TRUE
"""

_KOMMUNE_NAMES_SQL = """
SELECT DISTINCT ON (code) code, navn FROM "{staging}".dagi_kommuneinddeling
WHERE _deleted IS NOT TRUE AND code IS NOT NULL ORDER BY code
"""


def merge_polygons(geoms: list[str] | None) -> str | None:
    """fold a parcel's lodflade geojson polygons into one geometry: 1 -> Polygon, several ->
    MultiPolygon (their polygon coordinate arrays concatenated). None/empty -> None (unserved)."""
    if not geoms:
        return None
    polys: list = []
    for text in geoms:
        if not text:
            continue
        try:
            geo = json.loads(text)
        except (ValueError, TypeError):
            continue
        coords = geo.get("coordinates")
        if coords is None:
            continue
        # polygon coords is one polygon's rings; a multipolygon is already a list of them
        polys.extend(coords if geo.get("type") == "MultiPolygon" else [coords])
    if not polys:
        return None
    merged = (
        {"type": "Polygon", "coordinates": polys[0]}
        if len(polys) == 1
        else {"type": "MultiPolygon", "coordinates": polys}
    )
    return json.dumps(merged, ensure_ascii=False, separators=(",", ":"))


def matrikel_labels(
    matrikelnummer: str | None, ejerlavsnavn: str | None, ejerlavskode: str | None
) -> tuple[str | None, str | None]:
    """the display betegnelse + the folded label the betegnelse KNN ranks on (same serving fold)."""
    betegnelse = (
        f"{matrikelnummer} {ejerlavsnavn}" if matrikelnummer and ejerlavsnavn else matrikelnummer
    )
    folded = normalize(" ".join(filter(None, (matrikelnummer, ejerlavsnavn, ejerlavskode)))) or None
    return betegnelse, folded


def _tuple(r: asyncpg.Record, geometry: str | None, kommunenavn: str | None) -> tuple:
    matr, navn, kode = r["matrikelnummer"], r["ejerlavsnavn"], r["ejerlavskode"]
    betegnelse, folded = matrikel_labels(matr, navn, kode)
    return (
        r["jordstykke"],
        r["bfe"],
        matr,
        kode,
        navn,
        r["kommunekode"],
        kommunenavn,
        r["centroid"],
        geometry,
        betegnelse,
        folded,
        r["lifecycle"],
    )


async def load_matrikel(
    reader: asyncpg.Connection, writer: asyncpg.Connection, schema: str, *, staging: str = STAGING
) -> int:
    """derive gen.<schema>.matrikel from the staging mat join; returns the emitted parcel count. a
    current parcel still needs geometry (the floors catch upstream defects); a non-current one is
    kept with null geometry (a retired designation must survive for the history betegnelse KNN)."""
    print("[i] streaming matrikel...")
    kommune_names = {
        r["code"]: r["navn"] for r in await reader.fetch(_KOMMUNE_NAMES_SQL.format(staging=staging))
    }
    total = missing = no_bfe = koder = 0
    batch: list[tuple] = []
    async for r in stream(reader, _MATRIKEL_SQL.format(staging=staging)):
        geometry = merge_polygons(r["lodflade_geoms"])
        current = (r["lifecycle"] or CURRENT) == CURRENT
        if geometry is None and current:  # a current parcel without geometry is an unserved defect
            missing += 1
            continue
        if not r["bfe"]:  # bfe is NOT NULL: never stamp a dangling jordstykke
            no_bfe += 1
            continue
        if r["ejerlavskode"]:
            koder += 1
        batch.append(_tuple(r, geometry, kommune_names.get(r["kommunekode"])))
        if len(batch) >= _BATCH:
            await writer.copy_records_to_table("matrikel", records=batch, columns=MATRIKEL_COLUMNS)
            total += len(batch)
            batch.clear()
    if batch:
        await writer.copy_records_to_table("matrikel", records=batch, columns=MATRIKEL_COLUMNS)
        total += len(batch)
    if missing:
        print(f"[!] {missing} current jordstykker without lodflade geometry (skipped)")
    if no_bfe:
        print(f"[!] {no_bfe} jordstykker whose sfe carries no bfe (skipped)")
    if total and not koder:  # sniff + digit fallback both missed: betegnelse search degrades
        print("[!] no ejerlavskode resolved (Ejerlav column sniff + digit fallback missed)")
    print(f"[+] copied {total} matrikel")
    return total
