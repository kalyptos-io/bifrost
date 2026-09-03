"""PostgresAddressSource: a pure row-mapper test + a seeded postgres parity suite.

the integration tests need a dev postgres (compose) and run against a throwaway gen_test_* schema
(created + dropped per fixture), so they never touch public - isolating them and dodging the
pytest-DSN-wipes-seed hazard. they skip unless BIFROST_DATABASE_DSN is set, and drive the real core
merge over the source, so they pin behaviour (dedup, union-bound recovery, ranking), not SQL.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
from bifrost.arms.aux_index import AuxMaps
from bifrost.arms.repository import PostgresAddressSource, _row, _Serving
from bifrost.composition import build_resolution
from bifrost.core.merge import merge
from bifrost.core.types import Decomposition, Search
from bifrost.db import ADDRESS_COLUMNS, ROAD_COLUMNS, STREET_DIM_COLUMNS, schema_sql

_DSN = os.environ.get("BIFROST_DATABASE_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="BIFROST_DATABASE_DSN unset")

_OLD_SEEDED = datetime(2026, 8, 11, 3, 0, 0, tzinfo=UTC)
_NEW_SEEDED = datetime(2026, 8, 12, 3, 0, 0, tzinfo=UTC)

# the belief branches over the seed's aux, exactly as a generation would carry them per request
_RESOLUTION = build_resolution(
    AuxMaps.from_rows(
        postcode_dim=["6900", "8000"],
        city_rows=[("skjern", "6900"), ("aarhus", "8000")],
        subloc_rows=[],
    )
)


def _search_for(**fields) -> Search:
    d = Decomposition(text="", **fields)
    return Search(
        beliefs=tuple(b for branch in _RESOLUTION.branches if (b := branch(d)) is not None)
    )


async def _make_source(seed) -> tuple[PostgresAddressSource, asyncpg.Pool, str]:
    # a fresh gen_test_* schema per fixture: apply schema.sql there, let `seed(conn)` COPY the rows,
    # then serve it via from_pool bound to that schema. never touches public.
    schema = f"gen_test_{uuid4().hex}"
    pool = await asyncpg.create_pool(_DSN, server_settings={"search_path": f"{schema}, public"})
    async with pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(schema_sql())
        await seed(conn)
    source = await PostgresAddressSource.from_pool(pool, generation=schema, seeded_at=_OLD_SEEDED)
    return source, pool, schema


async def _teardown(pool: asyncpg.Pool, schema: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await pool.close()


# ---- pure: record -> AddressRow mapper ----


def test_row_maps_record_fields():
    rec = {
        "address_id": "id-a",
        "street_id": 0,
        "street": "Lindealle",  # from the street_dim join
        "folded_street": "lindealle",
        "house_number": "13",
        "house_letter": None,
        "floor": None,
        "door": None,
        "postcode": "6900",
        "sub_locality": None,
        "city": "Skjern",
        "lifecycle": "current",
        "street_similarity": 0.8,
        "adgangspunkt_x": 722345.67,
        "adgangspunkt_y": 6179535.68,
        "vejpunkt_x": None,
        "vejpunkt_y": None,
    }
    row = _row(rec)
    assert (row.address_id, row.street_id, row.postcode, row.street, row.street_similarity) == (
        "id-a",
        0,
        "6900",
        "Lindealle",
        0.8,
    )
    assert (row.adgangspunkt_x, row.adgangspunkt_y) == (722345.67, 6179535.68)
    assert row.vejpunkt_x is None


async def test_connect_closes_pool_when_load_fails(monkeypatch):
    # a load_from failure must not orphan the pool connect() just opened. connect() first opens a
    # bootstrap conn + select_current to find the generation, then the serving pool -> stub both.
    from bifrost.arms import repository
    from bifrost.db.generations import Generation

    closed = False

    class _FakePool:
        async def close(self):
            nonlocal closed
            closed = True

    class _FakeConn:
        async def close(self): ...

    async def _fake_connect(*args, **kwargs):
        return _FakeConn()

    async def _fake_select_current(conn, **kwargs):
        return Generation("gen_x", "shape-x", 1, 1, 1, 1, 1, None)

    async def _fake_create_pool(*args, **kwargs):
        return _FakePool()

    async def _boom(pool):
        raise RuntimeError("load failed")

    monkeypatch.setattr(repository.asyncpg, "connect", _fake_connect)
    monkeypatch.setattr(repository.generations, "select_current", _fake_select_current)
    monkeypatch.setattr(repository.asyncpg, "create_pool", _fake_create_pool)
    monkeypatch.setattr(repository.StreetIndex, "load_from", _boom)
    with pytest.raises(RuntimeError, match="load failed"):
        await PostgresAddressSource.connect("postgresql://x")
    assert closed


# ---- serving refcount + snapshot pinning (no db) ----


class _FakePool:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _serving(pool, generation, version=1, seeded_at=_OLD_SEEDED):
    # indexes + resolution are untouched by acquire/release/retire/generation, dummies suffice here
    return _Serving(pool, object(), object(), object(), None, generation, version, seeded_at)


async def test_retire_closes_immediately_when_idle():
    pool = _FakePool()
    await _serving(pool, "gen_old").retire()
    assert pool.closed  # no outstanding snapshot -> close now


async def test_retire_defers_close_until_snapshot_released():
    pool = _FakePool()
    src = PostgresAddressSource(_serving(pool, "gen_old"))
    snap = src.snapshot()  # pins the serving (holds a ref)
    await src._serving.retire()  # a cutover retires the old serving mid-request
    assert not pool.closed  # a live snapshot keeps the pool open
    await snap.__aexit__(None, None, None)  # request done -> release the ref
    assert pool.closed  # last ref gone -> the retired pool closes


async def test_snapshot_pins_generation_across_a_swap():
    old_pool, new_pool = _FakePool(), _FakePool()
    src = PostgresAddressSource(_serving(old_pool, "gen_old"))
    async with src.snapshot() as snap:
        old = src._serving
        # cutover swaps the serving mid-request
        src._serving = _serving(new_pool, "gen_new", seeded_at=_NEW_SEEDED)
        await old.retire()
        assert snap.generation == "gen_old"  # the request stays pinned to its own generation
        assert snap.seeded_at == _OLD_SEEDED  # ...and to that generation's freshness stamp
        assert not old_pool.closed  # old pool alive while the snapshot references it
    assert old_pool.closed  # released on exit -> old pool closes


class _LeaseConn:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class _RefusingPool(_FakePool):
    # stands in for a pool aimed at a read replica: any write through it is a test failure
    async def execute(self, *_a, **_kw):
        raise AssertionError("lease write went through the serving pool")

    async def fetchval(self, *_a, **_kw):
        raise AssertionError("lease write went through the serving pool")


def _leased(monkeypatch, host="bifrost-pg-r"):
    """a source whose serving pool refuses every statement, so only a lease write off the dsn
    survives. returns the recorder the fake asyncpg.connect fills in."""
    from bifrost.arms import repository

    seen = {}

    async def fake_connect(dsn, **kw):
        seen["dsn"], seen["kwargs"] = dsn, kw
        seen["conn"] = _LeaseConn()
        return seen["conn"]

    monkeypatch.setattr(repository.asyncpg, "connect", fake_connect)
    src = PostgresAddressSource(_serving(_RefusingPool(), "gen_x"))
    src._params = repository._ConnParams("postgresql://u:p@bifrost-pg-rw/bifrost", host, 2, 10)
    return src, seen


async def test_lease_write_bypasses_the_serving_pool(monkeypatch):
    # database_host aims the serving pool at a replica service; the lease insert has to reach the
    # primary the dsn names, or it dies on "cannot execute INSERT in a read-only transaction"
    from bifrost.arms import repository

    src, seen = _leased(monkeypatch)
    written = {}

    async def heartbeat(conn, holder, schema):
        written["conn"], written["schema"] = conn, schema

    monkeypatch.setattr(repository.generations, "heartbeat", heartbeat)
    await src._beat("gen_x")
    assert seen["dsn"] == "postgresql://u:p@bifrost-pg-rw/bifrost"
    assert "host" not in seen["kwargs"]  # the read override must never reach a write
    assert written["conn"] is seen["conn"] and written["schema"] == "gen_x"
    assert seen["conn"].closed  # one connect per beat, released again straight away


async def test_lease_failure_is_counted_and_serving_continues(monkeypatch, caplog):
    # gc holds a schema for an hour past the last beat, so a failed write is an alert, not an
    # outage: it must not raise into the refresh loop and must not touch readiness
    from bifrost.arms import repository
    from prometheus_client import REGISTRY

    def failures():
        return REGISTRY.get_sample_value("bifrost_serving_lease_failures_total") or 0.0

    async def boom(*_a):
        raise asyncpg.InsufficientPrivilegeError("permission denied for table serving_leases")

    src, _ = _leased(monkeypatch)
    monkeypatch.setattr(repository.generations, "heartbeat", boom)
    before = failures()
    with caplog.at_level("WARNING"):
        await src._beat("gen_x")
    assert "serving-lease heartbeat failed" in caplog.text
    assert failures() == before + 1
    assert not hasattr(src, "lease_stale")  # readiness never consults the lease


async def test_lease_is_skipped_without_conn_params(monkeypatch):
    # from_pool sources borrow a caller's pool and know no dsn; they take no lease to begin with
    from bifrost.arms import repository

    async def no_connect(*_a, **_kw):
        raise AssertionError("a source without conn params must not dial the db")

    monkeypatch.setattr(repository.asyncpg, "connect", no_connect)
    await PostgresAddressSource(_serving(_RefusingPool(), "gen_x"))._beat("gen_x")


@_needs_db
async def test_tick_cuts_over_to_a_newer_generation():
    # a newer registered generation of the build shape -> _tick builds its pool and swaps _serving
    from bifrost.arms.repository import _ConnParams
    from bifrost.db import generations
    from bifrost.db.shape import build_fingerprint

    async def seed(conn):
        await conn.copy_records_to_table("addresses", records=_SEED, columns=_ADDR_COLS)
        await conn.copy_records_to_table(
            "street_dim", records=_STREET_DIM, columns=STREET_DIM_COLUMNS[:-1]
        )

    shape = build_fingerprint()
    reg = await asyncpg.connect(_DSN)
    a_schema = b_schema = a_pool = None
    try:
        src, a_pool, a_schema = await _make_source(seed)
        src._params = _ConnParams(_DSN, None, 2, 10)  # from_pool leaves this unset; _tick needs it
        await generations.register(
            reg, generations.Generation(a_schema, shape, 3, 0, 0, 0, 0, None)
        )

        # build b on the reg connection (no extra pool); _tick opens its own pool on b_schema
        b_schema = f"gen_test_{uuid4().hex}"
        await reg.execute(f'CREATE SCHEMA "{b_schema}"')
        await reg.execute(f'SET search_path TO "{b_schema}", public')
        await reg.execute(schema_sql())
        await seed(reg)
        await reg.execute("SET search_path TO public")
        await generations.register(
            reg, generations.Generation(b_schema, shape, 3, 0, 0, 0, 0, None)
        )

        await src._tick()  # sees b is newer -> cuts over
        assert src.generation == b_schema
        assert a_pool.is_closing()  # old pool retired + closed (no live snapshot held it)
        await src.close()
    finally:
        for s in (a_schema, b_schema):
            if s:
                await reg.execute(f'DROP SCHEMA IF EXISTS "{s}" CASCADE')
        # by our own schema names, not by shape: shape is the build fingerprint, shared with any
        # real generation on this db - a shape-wide delete would wipe those too
        await reg.execute(
            "DELETE FROM public.generations WHERE schema_name = ANY($1)", [a_schema, b_schema]
        )
        await reg.execute(
            "DELETE FROM public.serving_leases WHERE schema_name = ANY($1)", [a_schema, b_schema]
        )
        await reg.close()


# ---- seeded postgres parity ----

# addresses fact (ADDRESS_COLUMNS order): no street strings - those live in street_dim; trailing
# 4 are the access/road point coords (epsg:25832), only id-a is geocoded here
_SEED = [
    (
        "id-a",
        0,
        "6900",
        "13",
        None,
        None,
        None,
        None,
        722345.67,
        6179535.68,
        722340.0,
        6179530.0,
        "Skjern",
    ),  # noqa: E501
    ("id-a-unit", 0, "6900", "13", None, "1", "tv", None, None, None, None, None, "Skjern"),
    ("id-b", 1, "6900", "13", None, None, None, None, None, None, None, None, "Skjern"),
    ("id-c", 2, "8000", "13", None, None, None, None, None, None, None, None, "Aarhus"),
]
_STREET_DIM = [
    (0, "Lindealle", "lindealle"),
    (1, "Strandlodsvej", "strandlodsvej"),
    (2, "Aarhusgade", "aarhusgade"),
]
# the seed rows populate address_id..vejpunkt_y + city; the dagi/bfe codes + lifecycle default here
_ADDR_COLS = ADDRESS_COLUMNS[:12] + ("city",)


@pytest.fixture
async def source():
    async def seed(conn):
        await conn.copy_records_to_table("addresses", records=_SEED, columns=_ADDR_COLS)
        await conn.copy_records_to_table(
            "street_dim", records=_STREET_DIM, columns=STREET_DIM_COLUMNS[:-1]
        )
        await conn.executemany(
            "INSERT INTO street_postcode (street_id, postcode) VALUES ($1, $2)",
            [(0, "6900"), (1, "6900"), (2, "8000")],
        )

    src, pool, schema = await _make_source(seed)
    async with src.snapshot() as snap:  # tests read through a pinned snapshot, as requests do
        yield snap
    await _teardown(pool, schema)


async def _resolve(source, **fields):
    return await merge(_search_for(**fields), source)


@_needs_db
async def test_source_exposes_generation(source):
    assert source.generation.startswith("gen_test_")  # the pinned serving generation's schema_name


@_needs_db
async def test_exact_query_returns_deduped_access_address(source):
    res = await _resolve(source, street="lindealle", house_number="13", postcode="6900")
    ids = [c.address_id for c in res]
    assert res[0].address_id == "id-a"  # access-address representative, not the floor/door unit
    assert "id-a-unit" not in ids  # units collapse to the access address
    assert ids.count("id-a") == 1
    assert res[0].city == "Skjern"  # city off the fact row (not a render-time aux lookup)
    assert (res[0].adgangspunkt_x, res[0].vejpunkt_y) == (722345.67, 6179530.0)  # coords survive


@_needs_db
async def test_stream_collapse_units_drops_only_represented_units(source):
    # units collapse only when a base row represents them; base-less groups stream intact
    async with source._serving.pool.acquire() as conn:
        await conn.copy_records_to_table(
            "addresses",
            records=[
                (
                    "id-107d",
                    0,
                    "6900",
                    "107",
                    "D",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "Skjern",
                ),  # noqa: E501
                (
                    "id-107d-u",
                    0,
                    "6900",
                    "107",
                    "D",
                    "1",
                    "tv",
                    None,
                    None,
                    None,
                    None,
                    None,
                    "Skjern",
                ),  # noqa: E501
                (
                    "id-107e",
                    0,
                    "6900",
                    "107",
                    "E",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "Skjern",
                ),
                (
                    "id-200a",
                    0,
                    "6900",
                    "200",
                    None,
                    "1",
                    "tv",
                    None,
                    None,
                    None,
                    None,
                    None,
                    "Skjern",
                ),
                (
                    "id-200b",
                    0,
                    "6900",
                    "200",
                    None,
                    "2",
                    "th",
                    None,
                    None,
                    None,
                    None,
                    None,
                    "Skjern",
                ),
            ],
            columns=_ADDR_COLS,
        )
    ids = {
        r.address_id
        async for batch in source.street_stream("lindealle", cap=64, batch=16, collapse_units=True)
        for r in batch
    }
    assert {"id-a", "id-107d", "id-107e"} <= ids  # base rows represent their access address
    assert "id-a-unit" not in ids and "id-107d-u" not in ids  # represented units collapse to base
    assert {"id-200a", "id-200b"} <= ids  # base-less group streams intact (no base to represent it)


@_needs_db
async def test_garbage_street_recovered_via_flat_sets(source):
    res = await _resolve(source, street="zzzqqq", house_number="13", postcode="6900")
    assert "id-a" in [c.address_id for c in res]  # postcode recovery set recovers the dead street


@_needs_db
async def test_postcode_only_executes(source):
    res = await _resolve(source, postcode="6900")
    assert {c.address_id for c in res} == {"id-a", "id-b"}  # scores+executes, no empty-term crash


@_needs_db
async def test_lone_city_sources_only_its_own_postcodes(source):
    # husnr is judge-only: a lone city sources its postcodes via by_postcodes, never a national
    # husnr fan-out, so the wrong-city "13" is never fetched (not merely demoted)
    res = await _resolve(source, house_number="13", city="skjern")
    ids = [c.address_id for c in res]
    assert {"id-a", "id-b"} <= set(ids)  # the Skjern 13s, recovered via the locality's postcodes
    assert "id-c" not in ids  # 8000/Aarhus never enters the pool


@_needs_db
async def test_street_stream_descends_in_similarity(source):
    frontiers = [
        min(r.street_similarity for r in batch)
        async for batch in source.street_stream("lindealle", cap=64, batch=1)
    ]
    assert frontiers == sorted(frontiers, reverse=True)  # combos stream by descending similarity


@_needs_db
async def test_by_postcodes_unions_the_set(source):
    rows = await source.by_postcodes({"6900", "8000"}, None, None, cap=100)
    assert {r.postcode for r in rows} == {"6900", "8000"}  # ANY($) unions the fuzzy set


@_needs_db
async def test_by_postcodes_filters_to_the_husnr_match(source):
    # add a husnr-99 row on a street that exactly matches the query (highest similarity); recovery
    # filters to the believed husnr, so the differing-husnr row is excluded, not merely out-ranked.
    async with source._serving.pool.acquire() as conn:
        await conn.copy_records_to_table(
            "street_dim", records=[(3, "Lindealley", "lindealley")], columns=STREET_DIM_COLUMNS[:-1]
        )
        await conn.copy_records_to_table(
            "addresses",
            records=[
                (
                    "id-near",
                    3,
                    "6900",
                    "99",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    "Skjern",
                )  # noqa: E501
            ],
            columns=_ADDR_COLS,
        )
    rows = await source.by_postcodes({"6900"}, "lindealley", "13", cap=4000)
    assert {r.house_number for r in rows} == {"13"}  # only the husnr match is fetched
    assert "id-near" not in {r.address_id for r in rows}  # the husnr-99 row never enters the pool


# ---- road geometry: one feature per navngivenvej, ranked by street sim, postcode-overlap pin ----

_GEO_A = '{"type": "LineString", "coordinates": [[0.0, 0.0], [1.0, 1.0]]}'
_GEO_B = '{"type": "LineString", "coordinates": [[9.0, 9.0], [8.0, 8.0]]}'
_GEO_C = '{"type": "LineString", "coordinates": [[5.0, 5.0], [6.0, 6.0]]}'


@pytest.fixture
async def geo_source():
    async def seed(conn):
        # one name-collapsed street_id "Hovedgaden" -> two distinct physical roads; v2 itself spans
        # two postcodes (the grain the navngivenvej key fixes: one road, one feature). v0 is a
        # retired road sharing v1's postcode with a lower id: current reads must never pick it
        await conn.copy_records_to_table(
            "street_dim", records=[(0, "Hovedgaden", "hovedgaden")], columns=STREET_DIM_COLUMNS[:-1]
        )
        await conn.copy_records_to_table(
            "road",
            records=[
                ("v0", 0, ["6900"], _GEO_C, "retired"),
                ("v1", 0, ["6900"], _GEO_A, "current"),
                ("v2", 0, ["8000", "8260"], _GEO_B, "current"),
            ],
            columns=ROAD_COLUMNS,
        )

    src, pool, schema = await _make_source(seed)
    async with src.snapshot() as snap:
        yield snap
    await _teardown(pool, schema)


@_needs_db
async def test_street_features_one_feature_per_road(geo_source):
    roads = await geo_source.street_features("hovedgaden", cap=64)
    # same street_id, two distinct roads -> two features, each its own geometry + postcode set
    assert {(r.geometry, r.postcodes) for r in roads} == {
        (_GEO_A, ("6900",)),
        (_GEO_B, ("8000", "8260")),
    }
    assert all(r.name == "Hovedgaden" for r in roads)


@_needs_db
async def test_street_features_road_spans_postcodes_in_one_feature(geo_source):
    # the core fix: pinning one of a road's postcodes returns the whole road, all postcodes intact
    roads = await geo_source.street_features("hovedgaden", cap=64, postcodes={"8260"})
    assert [(r.geometry, r.postcodes) for r in roads] == [(_GEO_B, ("8000", "8260"))]


@_needs_db
async def test_street_features_postcode_confines_to_one_road(geo_source):
    roads = await geo_source.street_features("hovedgaden", cap=64, postcodes={"6900"})
    assert [(r.geometry, r.postcodes) for r in roads] == [(_GEO_A, ("6900",))]


@_needs_db
async def test_streets_by_names_batches_and_maps(geo_source):
    # rank in-proc, one road fetch, then each name's pin picks its own road
    roads = await geo_source.streets_by_names([("hovedgaden", {"6900"})])
    assert roads["hovedgaden"].geometry == _GEO_A and roads["hovedgaden"].postcodes == ("6900",)
    assert await geo_source.streets_by_names([("nope", None)]) == {}  # unranked name absent


@_needs_db
async def test_create_pool_refuses_a_backend_without_the_generation_row():
    # -r load-balances per connection, so a lagging standby would serve empty tables unchecked
    from bifrost.arms.repository import _ConnParams, _create_pool
    from bifrost.db import generations
    from bifrost.db.shape import build_fingerprint

    schema = f"gen_test_{uuid4().hex}"
    shape = build_fingerprint()
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        params = _ConnParams(_DSN, None, 1, 2)
        with pytest.raises(RuntimeError, match="not replayed"):
            await _create_pool(params, schema)

        await generations.register(conn, generations.Generation(schema, shape, 3, 0, 0, 0, 0, None))
        pool = await _create_pool(params, schema)
        await pool.close()
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.execute("DELETE FROM public.generations WHERE schema_name = $1", schema)
        await conn.execute("DELETE FROM public.serving_leases WHERE schema_name = $1", schema)
        await conn.close()
