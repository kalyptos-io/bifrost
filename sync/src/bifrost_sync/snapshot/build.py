"""gen_<ts> orchestration: schema.sql -> stage-1 lane barrier ({matrikel -> ejendom} || districts ||
areas || stednavne) -> address stream -> street_dim -> indexes -> street_postcode -> roads ->
aliases -> aux -> ANALYZE -> gates -> register -> gc -> watermark. a main reader/writer pair owns
the schema DDL + the address stream; each stage-1 lane opens its own tuned reader/writer pair.
session tuning + [i]/[+]/[-] logging ported from load.py.

every promotion gate runs *before* register(): absolute count floors, the shrink delta against the
previous generation, the skipped-row share, projection-column coverage, and the lifecycle-mapping
share. any violation aborts nonzero, leaving the previous generation serving (the retired
should_seed() gate moves here). unregistered gen_<ts> schemas are gc'd past a grace, so a mid-build
crash self-heals.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
from bifrost.db import ADDRESS_COLUMNS, STREET_DIM_COLUMNS, generations, schema_sql
from bifrost.db.shape import build_fingerprint

from ..config import Config
from . import STAGING
from . import meta as meta_mod
from .addresses import ensure_hist_indexes, iter_addresses
from .aliases import load_aliases
from .areas import load_areas
from .aux import Aux, acc_aux, write_aux_tables
from .districts import stamp_districts
from .ejendom import load_ejendom
from .lifecycle import CURRENT
from .matrikel import load_matrikel
from .records import (
    Counts,
    Floors,
    StreetIds,
    floor_violations,
    ratio_violations,
    shrink_violations,
    to_record,
)
from .report import log_report
from .roads import load_roads
from .stednavne import load_stednavne

# session-only bulk tuning; synchronous_commit off is safe (an unregistered gen is gc'd). the reader
# disables parallel gather - the streaming join otherwise resizes a /dev/shm segment past a
# container's small tmpfs (DiskFullError), and a cursor gains nothing from parallel workers anyway.
# shared with export.py (same reader/writer split over the same staging join).
WRITER_TUNE = "SET maintenance_work_mem='1GB'; SET work_mem='256MB'; SET synchronous_commit=off"
READER_TUNE = "SET max_parallel_workers_per_gather=0; SET work_mem='256MB'"

# secondary index defs from the catalog (schema.sql is the single source); dropped before bulk load,
# recreated after (build is costly). *_pkey excluded: constraint indexes, kept.
_CAPTURE_INDEXES = r"""
SELECT tablename, indexname, indexdef FROM pg_indexes
WHERE schemaname = $1 AND tablename IN ('addresses', 'street_dim', 'street_postcode', 'matrikel')
  AND indexname NOT LIKE '%\_pkey'
"""

# bridge = distinct (street_id, postcode); pure ids, no string columns
_SP_INSERT = (
    "INSERT INTO street_postcode (street_id, postcode) "
    "SELECT DISTINCT street_id, postcode FROM addresses"
)

_DEFAULT_FLOORS = Floors()


async def create_generation_schema(writer: asyncpg.Connection, schema: str) -> list[asyncpg.Record]:
    """virgin schema under the load search_path; capture + drop the secondary indexes for the bulk
    copy. returns the index defs to recreate afterward."""
    print(f"[i] building generation {schema}...")
    await writer.execute(f'CREATE SCHEMA "{schema}"')
    await writer.execute(f'SET search_path TO "{schema}", public')
    await writer.execute(schema_sql())
    indexes = await writer.fetch(_CAPTURE_INDEXES, schema)
    for r in indexes:
        await writer.execute(f'DROP INDEX IF EXISTS "{r["indexname"]}"')
    return indexes


@contextlib.asynccontextmanager
async def _lane(
    cfg: Config, schema: str
) -> AsyncIterator[tuple[asyncpg.Connection, asyncpg.Connection]]:
    """a lane's own tuned reader/writer pair, writer pinned to the gen schema; both closed on exit
    (close errors suppressed so a teardown never masks the lane's own failure)."""
    reader = writer = None
    try:
        reader = await asyncpg.connect(cfg.dsn)
        writer = await asyncpg.connect(cfg.dsn)
        await reader.execute(READER_TUNE)
        await writer.execute(WRITER_TUNE)
        await writer.execute(f'SET search_path TO "{schema}", public')
        yield reader, writer
    finally:
        for conn in (reader, writer):
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()


async def _matrikel_lane(
    cfg: Config, schema: str, indexes: list[asyncpg.Record], *, staging: str = STAGING
) -> None:
    async with _lane(cfg, schema) as (reader, writer):
        await load_matrikel(reader, writer, schema, staging=staging)  # before addresses (currency)
        await load_ejendom(reader, writer, schema, staging=staging)  # before ejendom_bfe
        # ejendom reads matrikel by pk only, so the gist rebuild hides under the sibling lanes
        for r in indexes:
            if r["tablename"] == "matrikel":
                await writer.execute(r["indexdef"])


async def _districts_lane(cfg: Config, schema: str, *, staging: str = STAGING) -> None:
    async with _lane(cfg, schema) as (reader, writer):
        await stamp_districts(reader, writer, schema, staging=staging)


async def _areas_lane(cfg: Config, schema: str, *, staging: str = STAGING) -> None:
    async with _lane(cfg, schema) as (reader, writer):
        await load_areas(reader, writer, schema, staging=staging)


async def _stednavne_lane(cfg: Config, schema: str, *, staging: str = STAGING) -> None:
    async with _lane(cfg, schema) as (reader, writer):
        await load_stednavne(reader, writer, schema, staging=staging)


async def load_addresses(
    reader: asyncpg.Connection,
    writer: asyncpg.Connection,
    schema: str,
    ids: StreetIds,
    *,
    aux: Aux | None = None,
    batch_size: int = 50_000,
    staging: str = STAGING,
) -> tuple[int, int]:
    """stream the address join through to_record into COPY batches; (copied, skipped). accrues the
    aux gazetteers over the same stream when one is passed."""
    print("[i] streaming addresses...")
    total = skipped = 0
    batch: list[tuple] = []
    async for o in iter_addresses(reader, schema, staging=staging):
        if aux is not None and (o.get("lifecycle") or CURRENT) == CURRENT:  # aux stays current-only
            acc_aux(o, aux)
        rec = to_record(o, ids)
        if rec is None:
            skipped += 1
            continue
        batch.append(rec)
        if len(batch) >= batch_size:
            await writer.copy_records_to_table("addresses", records=batch, columns=ADDRESS_COLUMNS)
            total += len(batch)
            batch.clear()
    if batch:
        await writer.copy_records_to_table("addresses", records=batch, columns=ADDRESS_COLUMNS)
        total += len(batch)
    print(f"[+] copied {total} addresses ({skipped} skipped, {len(ids)} distinct streets)")
    return total, skipped


async def load_street_dim(writer: asyncpg.Connection, ids: StreetIds) -> None:
    print("[i] copying street_dim...")  # fully known only after the address stream
    await writer.copy_records_to_table(
        "street_dim", records=ids.dim_records(), columns=STREET_DIM_COLUMNS
    )


async def _run_index(cfg: Config, indexdef: str) -> None:
    """build one schema-qualified index on a transient tuned writer (no search_path needed)."""
    conn = await asyncpg.connect(cfg.dsn)
    try:
        await conn.execute(WRITER_TUNE)
        await conn.execute(indexdef)
    finally:
        with contextlib.suppress(Exception):
            await conn.close()


async def build_street_postcode(
    cfg: Config, writer: asyncpg.Connection, indexes: list[asyncpg.Record]
) -> int:
    print("[i] recreating addresses/street_dim indexes...")
    async with asyncio.TaskGroup() as tg:  # each on its own connection; defs are schema-qualified
        for r in indexes:
            if r["tablename"] in ("addresses", "street_dim"):
                tg.create_task(_run_index(cfg, r["indexdef"]))
    print("[i] building street_postcode bridge...")
    await writer.execute(_SP_INSERT)
    for r in indexes:
        if r["tablename"] == "street_postcode":
            await writer.execute(r["indexdef"])
    return await writer.fetchval("SELECT count(*) FROM street_postcode")


# the floors gate CURRENT-lifecycle counts so their calibrated meaning survives the lifecycle rework
async def _gather_counts(writer: asyncpg.Connection) -> Counts:
    async def n(sql: str) -> int:
        return await writer.fetchval(sql)

    return Counts(
        addresses=await n("SELECT count(*) FROM addresses WHERE lifecycle = 'current'"),
        areas=await n("SELECT count(*) FROM admin_area WHERE lifecycle = 'current'"),
        matrikel=await n("SELECT count(*) FROM matrikel WHERE lifecycle = 'current'"),
        stednavne=await n("SELECT count(*) FROM stednavne WHERE lifecycle = 'current'"),
        ejendom=await n("SELECT count(*) FROM ejendom WHERE lifecycle = 'current'"),
        sfe=await n(
            "SELECT count(*) FROM ejendom WHERE type = 'samlet_fast_ejendom' "
            "AND lifecycle = 'current'"
        ),
        ejerlejlighed=await n(
            "SELECT count(*) FROM ejendom WHERE type = 'ejerlejlighed' AND lifecycle = 'current'"
        ),
        bpfg=await n(
            "SELECT count(*) FROM ejendom WHERE type = 'bygning_paa_fremmed_grund' "
            "AND lifecycle = 'current'"
        ),
        ebr_stamped=await n(
            "SELECT count(*) FROM addresses a JOIN ejendom e ON e.bfe = a.ejendom_bfe "
            "WHERE e.type = 'ejerlejlighed' AND a.lifecycle = 'current'"
        ),
        aux_postcode_dim=await n("SELECT count(*) FROM aux_postcode_dim"),
    )


# the address projection columns a downstream register/projection reads; one going all-null ships
# green otherwise (the regionskode hole emptied every region projection)
_COVERAGE_COLUMNS = ("kommunekode", "regionskode", "sognekode", "postcode", "ejendom_bfe")


async def _gather_coverage(writer: asyncpg.Connection) -> dict[str, tuple[int, int]]:
    """{column: (nulls, current rows)} over the address projection columns."""
    nulls = ", ".join(f"count(*) - count({c}) AS {c}" for c in _COVERAGE_COLUMNS)
    row = await writer.fetchrow(
        f"SELECT count(*) AS total, {nulls} FROM addresses WHERE lifecycle = 'current'"
    )
    return {f"addresses.{c}": (row[c], row["total"]) for c in _COVERAGE_COLUMNS}


async def _prior_generation(conn: asyncpg.Connection) -> generations.Generation | None:
    gens = await generations.all_generations(conn)
    return max(gens, key=lambda g: g.seeded_at) if gens else None


async def _gc(writer: asyncpg.Connection) -> None:
    # db-clock authority for gc grace + lease window; never drop a live-leased schema
    now = await writer.fetchval("SELECT now()")
    held = await generations.held_schemas(writer)
    schemas = await generations.existing_schemas(writer)
    for target in generations.gc_targets(
        schemas, await generations.all_generations(writer), now, held=held
    ):
        print(f"[-] gc dropping old generation {target}")
        await writer.execute("DELETE FROM public.generations WHERE schema_name = $1", target)
        await writer.execute(f'DROP SCHEMA IF EXISTS "{target}" CASCADE')
    await generations.prune_leases(writer)  # dead-holder leases grow unbounded otherwise


async def build_generation(
    cfg: Config,
    *,
    cursors: dict[str, int],
    contracts: dict[str, str] | None = None,
    floors: Floors = _DEFAULT_FLOORS,
    batch_size: int = 50_000,
    staging: str = STAGING,
    shape: str | None = None,
    schema: str | None = None,
    gc: bool = True,
    allow_shrink: bool = False,
) -> str:
    """derive one gen_<ts> from staging and register it (the atomic cutover). raises SystemExit on a
    gate violation, before register, so the live generation keeps serving. returns the schema.

    shape/schema/gc are isolation seams (defaults are production): an integration test injects a
    unique shape (invisible to serving + shape-scoped gc), a gen_test_* schema, and gc=False."""
    schema_ts = datetime.now(UTC)  # schema-name label; seeded_at ordering is db-clock (register)
    schema = schema or generations.schema_name_for(schema_ts)
    aux = Aux()  # intrinsic; accrued over the address stream, written into the gen aux tables
    reader = await asyncpg.connect(cfg.dsn)
    writer = await asyncpg.connect(cfg.dsn)
    try:
        await reader.execute(READER_TUNE)
        await writer.execute(WRITER_TUNE)
        await ensure_hist_indexes(writer, staging=staging)
        indexes = await create_generation_schema(writer, schema)

        # stage 1 lanes are mutually independent; matrikel before addresses (jordstykke currency)
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_matrikel_lane(cfg, schema, indexes, staging=staging))
            tg.create_task(_districts_lane(cfg, schema, staging=staging))
            tg.create_task(_areas_lane(cfg, schema, staging=staging))
            tg.create_task(_stednavne_lane(cfg, schema, staging=staging))

        ids = StreetIds()
        total, skipped = await load_addresses(
            reader, writer, schema, ids, aux=aux, batch_size=batch_size, staging=staging
        )
        await load_street_dim(writer, ids)
        combos = await build_street_postcode(cfg, writer, indexes)
        print(f"[+] {combos} street_postcode combos")
        await writer.execute(f'DROP TABLE IF EXISTS "{schema}"._district_stamp')

        await load_roads(reader, writer, schema, ids, staging=staging)
        aliases = await load_aliases(reader, writer, schema, staging=staging)  # needs road/area/mat
        await write_aux_tables(writer, schema, aux)  # before register, search_path still gen schema

        await writer.execute("ANALYZE addresses, street_dim, street_postcode, matrikel, ejendom")
        counts = await _gather_counts(writer)
        coverage = await _gather_coverage(writer)
        lifecycle = await log_report(reader, writer, schema, aliases, staging=staging)
        gen = generations.Generation(
            schema,
            shape or build_fingerprint(),
            total,
            counts.areas,
            counts.matrikel,
            counts.stednavne,
            counts.ejendom,
            schema_ts,  # ignored on write; register stamps seeded_at from the db clock
        )
        dropped = {"skipped_addresses": (skipped, total + skipped)}
        violations = [
            *floor_violations(counts, floors),
            *ratio_violations(dropped, floors.max_skipped),
            *ratio_violations(coverage, floors.max_null),
            *ratio_violations(lifecycle, floors.max_unmapped),
        ]
        if not allow_shrink:
            violations += shrink_violations(gen, await _prior_generation(writer), floors)
        if violations:
            raise SystemExit(f"[-] promotion gates failed, aborting before register: {violations}")

        await writer.execute("SET search_path TO public")  # register/gc touch public.generations
        await generations.register(writer, gen)
        print(f"[+] registered generation {schema} ({total} addresses)")
        if gc:
            await _gc(writer)
        await meta_mod.stamp_watermark(writer, cursors, contracts or {}, staging=staging)
    finally:
        await reader.close()
        await writer.close()
    return schema
