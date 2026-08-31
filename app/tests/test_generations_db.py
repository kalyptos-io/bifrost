"""generations registry + serving-lease integration against a real postgres.

these drive public.generations / public.serving_leases directly, so they use a unique per-test fake
shape + gen_test_* schemas and clean those up in teardown - they never read or drop a real seed's
rows/schemas (gc_targets selection itself is covered purely in test_generations). skip w/o a dsn.
"""

import os
from datetime import timedelta
from uuid import uuid4

import asyncpg
import pytest
from bifrost.db import generations
from bifrost.db.contracts import Contract
from bifrost.db.generations import Generation

_DSN = os.environ.get("BIFROST_DATABASE_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="BIFROST_DATABASE_DSN unset")


def _gen(schema: str, shape: str, *, ejendom: int = 1, version: int = 1) -> Generation:
    # seeded_at ignored on write (db stamps it)
    return Generation(schema, shape, 10, 1, 1, 1, ejendom, None, version)


async def _make_schema(conn: asyncpg.Connection) -> str:
    schema = f"gen_test_{uuid4().hex}"
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    return schema


async def _cleanup(conn: asyncpg.Connection, shapes: list[str], schemas: list[str], holder: str):
    for s in schemas:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
    await conn.execute(generations.GENERATIONS_DDL)  # ensure the tables exist before deleting
    await conn.execute(generations.LEASES_DDL)
    await conn.execute("DELETE FROM public.generations WHERE shape = ANY($1)", shapes)
    await conn.execute("DELETE FROM public.serving_leases WHERE holder = $1", holder)


@_needs_db
async def test_register_stamps_db_clock_and_ignores_client_ts():
    conn = await asyncpg.connect(_DSN)
    shape = f"test-{uuid4().hex}"
    schemas: list[str] = []
    try:
        schema = await _make_schema(conn)
        schemas.append(schema)
        seeded = await generations.register(conn, _gen(schema, shape))  # client seeded_at is None
        assert seeded is not None  # the db DEFAULT now() filled it despite the None in the object
        row = await conn.fetchrow(
            "SELECT seeded_at FROM public.generations WHERE schema_name = $1", schema
        )
        assert row["seeded_at"] == seeded
        # register runs under sync's privileged role, so it provisions the lease table too: a
        # read-only app role never needs CREATE just to heartbeat
        assert await conn.fetchval("SELECT to_regclass('public.serving_leases')") is not None
    finally:
        await _cleanup(conn, [shape], schemas, "")
        await conn.close()


@_needs_db
async def test_select_current_returns_newest_by_db_clock():
    conn = await asyncpg.connect(_DSN)
    shape = f"test-{uuid4().hex}"
    schemas: list[str] = []
    try:
        older, newer = await _make_schema(conn), await _make_schema(conn)
        schemas += [older, newer]
        await generations.register(conn, _gen(older, shape))
        await generations.register(conn, _gen(newer, shape))
        # backdate `older` so ordering can't lean on insert/schema-name order
        await conn.execute(
            "UPDATE public.generations SET seeded_at = now() - interval '1 hour' "
            "WHERE schema_name = $1",
            older,
        )
        gen = await generations.select_current(conn, current=Contract(1, shape))
        assert gen is not None and gen.schema_name == newer
    finally:
        await _cleanup(conn, [shape], schemas, "")
        await conn.close()


@_needs_db
async def test_select_current_prefers_current_over_newer_previous():
    # the current pair wins even when a previous-contract row is strictly newer by the db clock
    conn = await asyncpg.connect(_DSN)
    base = uuid4().hex
    cur, prev = Contract(2, f"cur-{base}"), Contract(1, f"prev-{base}")
    schemas: list[str] = []
    try:
        cur_s, prev_s = await _make_schema(conn), await _make_schema(conn)
        schemas += [cur_s, prev_s]
        await generations.register(conn, _gen(cur_s, cur.fingerprint, version=cur.version))
        await generations.register(conn, _gen(prev_s, prev.fingerprint, version=prev.version))
        await conn.execute(
            "UPDATE public.generations SET seeded_at = now() + interval '1 hour' "
            "WHERE schema_name = $1",
            prev_s,
        )
        gen = await generations.select_current(conn, current=cur, previous=prev)
        assert gen is not None and gen.schema_name == cur_s
    finally:
        await _cleanup(conn, [cur.fingerprint, prev.fingerprint], schemas, "")
        await conn.close()


@_needs_db
async def test_select_current_falls_back_to_previous():
    conn = await asyncpg.connect(_DSN)
    base = uuid4().hex
    cur, prev = Contract(2, f"cur-{base}"), Contract(1, f"prev-{base}")
    schemas: list[str] = []
    try:
        prev_s = await _make_schema(conn)
        schemas.append(prev_s)
        await generations.register(conn, _gen(prev_s, prev.fingerprint, version=prev.version))
        gen = await generations.select_current(conn, current=cur, previous=prev)
        assert (
            gen is not None and gen.schema_name == prev_s and gen.contract_version == prev.version
        )
    finally:
        await _cleanup(conn, [prev.fingerprint], schemas, "")
        await conn.close()


@_needs_db
async def test_gc_contract_retention_over_real_rows():
    # retention over real rows: current pair keeps its 2 newest, a retired pair keeps only its
    # newest, and a live lease spares even a drop target
    conn = await asyncpg.connect(_DSN)
    base = uuid4().hex
    cur, retired = Contract(2, f"cur-{base}"), f"retired-{base}"
    schemas: list[str] = []
    try:
        keep1, keep2, drop, retired_old, retired_new = [await _make_schema(conn) for _ in range(5)]
        schemas += [keep1, keep2, drop, retired_old, retired_new]
        for s in (keep1, keep2, drop):
            await generations.register(conn, _gen(s, cur.fingerprint, version=cur.version))
        for s in (retired_old, retired_new):
            await generations.register(conn, _gen(s, retired, version=1))
        await conn.execute(
            "UPDATE public.generations SET seeded_at = now() - interval '3 hours' "
            "WHERE schema_name = ANY($1)",
            schemas,
        )
        await conn.execute(
            "UPDATE public.generations SET seeded_at = now() - interval '5 hours' "
            "WHERE schema_name = ANY($1)",
            [drop, retired_old],  # strictly oldest in each pair -> past that pair's keep set
        )
        now = await conn.fetchval("SELECT now()")
        regs = [g for g in await generations.all_generations(conn) if g.schema_name in schemas]
        targets = generations.gc_targets(schemas, regs, now, current=cur, previous=None)
        assert set(targets) == {drop, retired_old}
        held = frozenset({drop})
        spared = generations.gc_targets(schemas, regs, now, current=cur, previous=None, held=held)
        assert spared == [retired_old]  # the lease pins the drop target
    finally:
        await _cleanup(conn, [cur.fingerprint, retired], schemas, "")
        await conn.close()


@_needs_db
async def test_register_round_trips_ejendom_count():
    conn = await asyncpg.connect(_DSN)
    shape = f"test-{uuid4().hex}"
    schemas: list[str] = []
    try:
        schema = await _make_schema(conn)
        schemas.append(schema)
        await generations.register(conn, _gen(schema, shape, ejendom=2_700_000))
        gen = await generations.select_current(conn, current=Contract(1, shape))
        assert gen is not None and gen.ejendom_count == 2_700_000
    finally:
        await _cleanup(conn, [shape], schemas, "")
        await conn.close()


@_needs_db
async def test_register_round_trips_contract_version():
    conn = await asyncpg.connect(_DSN)
    contract = Contract(2, f"test-{uuid4().hex}")
    schemas: list[str] = []
    try:
        schema = await _make_schema(conn)
        schemas.append(schema)
        await generations.register(
            conn, _gen(schema, contract.fingerprint, version=contract.version)
        )
        gen = await generations.select_current(conn, current=contract)
        assert gen is not None and gen.contract_version == 2
    finally:
        await _cleanup(conn, [contract.fingerprint], schemas, "")
        await conn.close()


@_needs_db
async def test_register_is_idempotent_across_registers():
    # register re-runs the idempotent create-table ddl each time; a second register must not error
    conn = await asyncpg.connect(_DSN)
    shape = f"test-{uuid4().hex}"
    schemas: list[str] = []
    try:
        first, second = await _make_schema(conn), await _make_schema(conn)
        schemas += [first, second]
        await generations.register(conn, _gen(first, shape))
        await generations.register(conn, _gen(second, shape))
        got = {g.schema_name for g in await generations.all_generations(conn) if g.shape == shape}
        assert got == {first, second}
    finally:
        await _cleanup(conn, [shape], schemas, "")
        await conn.close()


@_needs_db
async def test_lease_heartbeat_held_and_expiry():
    # finding [4]: a live lease pins a schema against gc; a stale one no longer does
    conn = await asyncpg.connect(_DSN)
    holder = f"test:{uuid4().hex}"
    schema = f"gen_test_{uuid4().hex}"
    try:
        await conn.execute(generations.LEASES_DDL)  # register() owns the DDL, not the heartbeat
        await generations.heartbeat(conn, holder, schema)
        assert schema in await generations.held_schemas(conn)  # fresh -> held
        await conn.execute(
            "UPDATE public.serving_leases SET heartbeat = now() - interval '2 hours' "
            "WHERE holder = $1",
            holder,
        )
        held = await generations.held_schemas(conn, window=timedelta(hours=1))
        assert schema not in held  # aged past the window -> no longer held
        await generations.drop_lease(conn, holder)
        assert schema not in await generations.held_schemas(conn, window=timedelta(days=3650))
    finally:
        await _cleanup(conn, [], [], holder)
        await conn.close()


@_needs_db
async def test_prune_leases_drops_only_stale_holders():
    # gc prunes leases past the window (oom-killed pods never self-clear); live ones survive
    conn = await asyncpg.connect(_DSN)
    live, dead = f"test:{uuid4().hex}", f"test:{uuid4().hex}"
    schema = f"gen_test_{uuid4().hex}"
    try:
        await conn.execute(generations.LEASES_DDL)  # register() owns the DDL, not the heartbeat
        await generations.heartbeat(conn, live, schema)
        await generations.heartbeat(conn, dead, schema)
        await conn.execute(
            "UPDATE public.serving_leases SET heartbeat = now() - interval '2 hours' "
            "WHERE holder = $1",
            dead,
        )
        await generations.prune_leases(conn, window=timedelta(hours=1))
        holders = {
            r["holder"]
            for r in await conn.fetch(
                "SELECT holder FROM public.serving_leases WHERE holder = ANY($1)", [live, dead]
            )
        }
        assert holders == {live}  # stale holder pruned, live one kept
    finally:
        await conn.execute(generations.LEASES_DDL)
        await conn.execute("DELETE FROM public.serving_leases WHERE holder = ANY($1)", [live, dead])
        await conn.close()
