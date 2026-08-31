"""admin_area: dagi administrative/postal areas, one row per code at the served generalization
scale, plus synthesized city polygons. ports the retired registry._area_records / _city_records.

each dagi area ships at several scales (id_namespace suffix); serve 1:500k, else the nearest,
full-res (fallback) last. the scale pick is a DISTINCT ON per code ordered by the scale rank. dagi
ships no city layer, so cities are the shapely union of each postdistrikt's postnummer polygons
grouped by navn (the address's carried city, what the area gazetteer ranks on).
"""

from __future__ import annotations

import math

import asyncpg
from bifrost.arms.normalize import normalize
from bifrost.core.types import LIFECYCLE_ORDER
from bifrost.db import ADMIN_AREA_COLUMNS

from . import STAGING
from .lifecycle import CURRENT, dagi_case

# serve 1:500k, then the nearest available, full-res ("dagi", multi-mb) last as a fallback
_AREA_SCALE_PREF = ("dagi0500k", "dagi0250k", "dagi1000k", "dagi2000k", "dagi")

# staging dagi table -> served area kind
_AREA_KINDS = {
    "dagi_kommuneinddeling": "kommune",
    "dagi_regionsinddeling": "region",
    "dagi_sogneinddeling": "sogn",
    "dagi_postnummerinddeling": "postcode",
    "dagi_retskreds": "retskreds",
    "dagi_politikreds": "politikreds",
    "dagi_opstillingskreds": "opstillingskreds",
}
_CITY_TABLE = "dagi_postnummerinddeling"  # postnummer polygons union into cities

# tolerance = sqrt(area)/k, not a fixed metre value: holds relative error constant across the sizes
_SIMPLIFY_K = 2000

# rank the scale token (last id_namespace path segment) by _AREA_SCALE_PREF; unknown scales last
_RANK = (
    f"COALESCE(array_position(ARRAY[{','.join(repr(s) for s in _AREA_SCALE_PREF)}], "
    f"regexp_replace(id_namespace, '^.*/', '')), {len(_AREA_SCALE_PREF) + 1})"
)
_LIFECYCLE_RANK = f"array_position(ARRAY[{','.join(repr(v) for v in LIFECYCLE_ORDER)}], lifecycle)"

# one row per code (id fallback where codeless): current-effective wins over the retired/preliminary
# versions deltas accrue, then best scale. lifecycle is virkning-based (dagi has no status).
_AREA_SQL = f"""
SELECT DISTINCT ON (COALESCE(code, id)) id, navn, code, geometry, lifecycle
FROM (
    SELECT id, navn, code, geometry, id_namespace,
        {dagi_case("virkningfra", "virkningtil")} AS lifecycle
    FROM "{{staging}}".{{table}}
    WHERE _deleted IS NOT TRUE AND navn IS NOT NULL AND geometry IS NOT NULL
) s
ORDER BY COALESCE(code, id), {_LIFECYCLE_RANK}, {_RANK}
"""


def simplify_geojson(geo: str) -> str:
    """douglas-peucker at sqrt(area)/_SIMPLIFY_K; non-polygons and bad input pass through."""
    from shapely import force_2d, from_geojson, to_geojson

    try:
        g = force_2d(from_geojson(geo))
        if not g.area:
            return geo
        return to_geojson(g.simplify(math.sqrt(g.area) / _SIMPLIFY_K, preserve_topology=True))
    except Exception:
        return geo


def city_polygons(rows: list[asyncpg.Record | dict]) -> list[tuple[str, str]]:
    """(navn, geojson) per city: the union of a postdistrikt's served-scale postnummer polygons,
    grouped by navn. one bad postdistrikt is skipped, not the whole pass."""
    from shapely import force_2d, from_geojson, to_geojson, union_all

    groups: dict[str, list[str]] = {}
    for r in rows:
        navn, geo = r["navn"], r["geometry"]
        if navn and geo:
            groups.setdefault(navn, []).append(geo)
    out: list[tuple[str, str]] = []
    for navn, geos in groups.items():
        geoms = []
        for g in geos:
            try:
                geoms.append(force_2d(from_geojson(g)))
            except Exception:
                continue
        if not geoms:
            continue
        try:
            union = union_all(geoms)
        except Exception:  # one bad postdistrikt must not sink the whole areas pass
            print(f"[!] city union failed for {navn} ({len(geoms)} polygons); skipped")
            continue
        out.append((navn, simplify_geojson(to_geojson(union))))
    return out


async def load_areas(
    reader: asyncpg.Connection, writer: asyncpg.Connection, schema: str, *, staging: str = STAGING
) -> int:
    """derive gen.<schema>.admin_area from the staging dagi tables + synthesized cities; returns the
    total row count."""
    print("[i] copying admin_area...")
    total = 0
    for table, kind in _AREA_KINDS.items():
        if not await reader.fetchval("SELECT to_regclass($1)", f"{staging}.{table}"):
            print(f"[!] {table} absent; {kind} areas skipped")
            continue
        rows = await reader.fetch(_AREA_SQL.format(staging=staging, table=table))
        batch = [
            (
                r["id"],
                kind,
                r["code"],
                r["navn"],
                normalize(r["navn"]),
                simplify_geojson(r["geometry"]),
                r["lifecycle"],
            )
            for r in rows
        ]
        if batch:
            await writer.copy_records_to_table(
                "admin_area", records=batch, columns=ADMIN_AREA_COLUMNS
            )
            total += len(batch)
        if table == _CITY_TABLE:
            cities = [
                (f"city:{navn}", "city", None, navn, normalize(navn), geo, CURRENT)
                for navn, geo in city_polygons(rows)
            ]
            if cities:
                await writer.copy_records_to_table(
                    "admin_area", records=cities, columns=ADMIN_AREA_COLUMNS
                )
                total += len(cities)
    print(f"[+] copied {total} admin areas")
    return total
