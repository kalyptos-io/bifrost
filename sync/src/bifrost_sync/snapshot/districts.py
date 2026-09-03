"""district stamp: retskreds/politikreds/opstillingskreds by point-in-polygon on the husnummer
adgangspunkt (they carry no address-joinable code). kommune/sogn come from husnummer refs and
region chains off the kommune row, so those never reach here.

full-res dagi polygons (built from staging geojson text, not wkt), epsg:25832 planar test. results
land in an UNLOGGED gen_<ts>._district_stamp (husnummer_id pk + 3 codes) that the address join
LEFT JOINs and build.py drops afterward. shapely is a snapshot-only dep - never in the served app.

the point-in-polygon fans out across a fork pool; workers inherit the built strtrees via fork
(never pickled), and each husnummer_id lands in exactly one shard so the merge equals serial output.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor

import asyncpg

from . import STAGING
from .read import stream

# fork-inherited by pool workers; set before the pool spawns, never pickled (strtree pickles by
# rebuild, so fork inheritance avoids shipping ~128 full-res polygons per worker)
_WORKER_TREES: list[tuple | None] | None = None

# point-in-polygon uses the full-res ("dagi") polygon, not the 1:500k served one - border accuracy
# matters per-address (opstillingskreds is fine-grained). the code repeats per generalization scale.
_PIP_SCALE_PREF = ("dagi", "dagi0250k", "dagi0500k", "dagi1000k", "dagi2000k")

# staging tables in the _district_stamp column order (retskreds, politikreds, opstillingskreds)
_DISTRICT_TABLES = ("dagi_retskreds", "dagi_politikreds", "dagi_opstillingskreds")
_STAMP_COLS = (
    "husnummer_id",
    "retskredsnummer",
    "politikredsnummer",
    "opstillingskredsnummer",
)

_STAMP_DDL = """
CREATE UNLOGGED TABLE "{schema}"._district_stamp (
    husnummer_id           text PRIMARY KEY,
    retskredsnummer        text,
    politikredsnummer      text,
    opstillingskredsnummer text
)
"""

_POINTS_SQL = """
SELECT h.id, ap.x, ap.y
FROM "{staging}".dar_husnummer h
JOIN "{staging}".dar_adressepunkt ap ON ap.id = h.adgangspunkt AND ap._deleted IS NOT TRUE
WHERE h._deleted IS NOT TRUE AND ap.x IS NOT NULL AND ap.y IS NOT NULL
"""


def _scale_rank(id_namespace: str | None) -> int:
    token = (id_namespace or "").rsplit("/", 1)[-1]
    return _PIP_SCALE_PREF.index(token) if token in _PIP_SCALE_PREF else len(_PIP_SCALE_PREF)


def district_geoms(rows: list[dict]) -> tuple[list, list[str]]:
    """one full-res shapely polygon per district code, finest scale wins. one malformed geometry is
    dropped, not the whole kind."""
    from shapely import from_geojson

    best: dict[str, tuple[int, str]] = {}  # code -> (scale rank, geojson text)
    for r in rows:
        code, geo = r.get("code"), r.get("geometry")
        if not code or not geo:
            continue
        rank = _scale_rank(r.get("id_namespace"))
        if code not in best or rank < best[code][0]:
            best[code] = (rank, geo)
    geoms, codes = [], []
    for code, (_, geo) in best.items():
        try:
            g = from_geojson(geo)
        except Exception:
            continue
        if not g.is_empty:
            geoms.append(g)
            codes.append(code)
    return geoms, codes


def stamp_chunk(
    trees: list[tuple | None],
    ids: list[str],
    xs: list[float],
    ys: list[float],
    out: dict[str, list[str | None]],
) -> None:
    """accumulate district codes for a chunk of points; first containing polygon per kind wins."""
    from shapely import points

    pts = points(xs, ys)
    for col, tree in enumerate(trees):
        if tree is None:
            continue
        strtree, codes = tree
        # covered_by, not within: within excludes the boundary, dropping a point that lands exactly
        # on a shared border in the gap-free tiling; covered_by gives it one neighbour
        pi, ti = strtree.query(pts, predicate="covered_by")
        for a, b in zip(pi.tolist(), ti.tolist(), strict=True):
            rec = out.setdefault(ids[a], [None, None, None])
            if rec[col] is None:  # kinds don't self-overlap
                rec[col] = codes[b]


async def _build_trees(reader: asyncpg.Connection, staging: str) -> list[tuple | None]:
    from shapely import STRtree

    trees: list[tuple | None] = []
    for table in _DISTRICT_TABLES:
        if not await reader.fetchval("SELECT to_regclass($1)", f"{staging}.{table}"):
            print(f"[!] {table} absent; district left unstamped")
            trees.append(None)
            continue
        rows = await reader.fetch(
            f'SELECT code, id_namespace, geometry FROM "{staging}".{table} '
            "WHERE _deleted IS NOT TRUE"
        )
        geoms, codes = district_geoms([dict(r) for r in rows])
        if not geoms:
            print(f"[!] no {table} polygons parsed; district left unstamped")
            trees.append(None)
            continue
        trees.append((STRtree(geoms), codes))
    return trees


def _stamp_shard(
    ids: list[str], xs: list[float], ys: list[float]
) -> list[tuple[str, str | None, str | None, str | None]]:
    """cpu-pure pool worker: reads the fork-inherited trees, touches no fd/loop/asyncpg."""
    out: dict[str, list[str | None]] = {}
    assert _WORKER_TREES is not None  # the pool initializer sets the trees before any worker call
    stamp_chunk(_WORKER_TREES, ids, xs, ys, out)
    return [(hid, rec[0], rec[1], rec[2]) for hid, rec in out.items()]


def _shard(
    ids: list[str], xs: list[float], ys: list[float], n: int
) -> list[tuple[list[str], list[float], list[float]]]:
    """contiguous whole-point slices (ceil division; last shard may be short)."""
    size = -(-len(ids) // n) or 1
    return [
        (ids[i : i + size], xs[i : i + size], ys[i : i + size]) for i in range(0, len(ids), size)
    ]


async def _flush(
    writer: asyncpg.Connection,
    schema: str,
    ex: ProcessPoolExecutor,
    workers: int,
    ids: list[str],
    xs: list[float],
    ys: list[float],
) -> int:
    shards = await asyncio.gather(
        *(
            asyncio.wrap_future(ex.submit(_stamp_shard, si, sx, sy))
            for si, sx, sy in _shard(ids, xs, ys, workers)
        )
    )
    records = [rec for shard in shards for rec in shard]  # shard order, disjoint husnummer_ids
    if not records:
        return 0
    await writer.copy_records_to_table(
        "_district_stamp", records=records, columns=_STAMP_COLS, schema_name=schema
    )
    return len(records)


async def stamp_districts(
    reader: asyncpg.Connection,
    writer: asyncpg.Connection,
    schema: str,
    *,
    staging: str = STAGING,
    chunk: int = 500_000,
) -> int:
    """build gen_<ts>._district_stamp: PIP the husnummer adgangspunkt against the three district
    polygons, in `chunk`-sized point batches (bounds the geos point-object peak)."""
    global _WORKER_TREES
    trees = await _build_trees(reader, staging)  # regular fetches, before the point cursor opens
    await writer.execute(_STAMP_DDL.format(schema=schema))
    if not any(trees):
        print("[-] no district polygons at all; _district_stamp left empty")
        return 0
    _WORKER_TREES = trees  # trees built (shapely imported) before fork so workers inherit both
    # process_cpu_count is affinity-aware but blind to a cgroup cpu quota; the chart sets the env
    env = os.environ.get("BIFROST_SYNC_WORKERS")
    workers = max(1, int(env) if env else (os.process_cpu_count() or 1) - 1)
    total = 0
    ids: list[str] = []
    xs: list[float] = []
    ys: list[float] = []
    try:
        # fork-safe: only the idle deadline thread is alive, no lock held; workers use shapely only
        with ProcessPoolExecutor(
            max_workers=workers, mp_context=multiprocessing.get_context("fork")
        ) as ex:
            async for r in stream(reader, _POINTS_SQL.format(staging=staging), prefetch=50_000):
                ids.append(r["id"])
                xs.append(r["x"])
                ys.append(r["y"])
                if len(ids) >= chunk:
                    total += await _flush(writer, schema, ex, workers, ids, xs, ys)
                    ids, xs, ys = [], [], []
            if ids:
                total += await _flush(writer, schema, ex, workers, ids, xs, ys)
    finally:
        _WORKER_TREES = None
    print(f"[+] stamped {total} husnummer districts")
    return total
