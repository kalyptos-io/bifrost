"""seeded postgres parity for the ejendom card queries (GeoSource.ejendom_*).

same convention as test_address_source: a throwaway gen_test_* schema per fixture (created +
dropped), never touching public, skipped unless BIFROST_DATABASE_DSN is set. drives the real SQL
against a small hand-built property graph, pinning card behaviour (chains, dedup, grouping).
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
from bifrost.arms.repository import PostgresAddressSource, _ejendom_geom
from bifrost.core.types import EjendomType
from bifrost.db import EJENDOM_COLUMNS, MATRIKEL_COLUMNS, schema_sql

_DSN = os.environ.get("BIFROST_DATABASE_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="BIFROST_DATABASE_DSN unset")

_SFE = "samlet_fast_ejendom"
_UNIT = "ejerlejlighed"
_BPFG = "bygning_paa_fremmed_grund"


def _parcel(jordstykke, bfe, matrikelnummer, ejerlavskode, betegnelse, geometry):
    # MATRIKEL_COLUMNS order; folded_betegnelse feeds the trigram search
    return (
        jordstykke,
        bfe,
        matrikelnummer,
        ejerlavskode,
        "Testby",
        "0100",
        "Kommune",
        "c",
        geometry,
        betegnelse,
        betegnelse.lower(),
    )


# ejerlav "500" spans bfes 1000/2000/500 (drives the ejerlav + dedup branches)
_PARCELS = [
    _parcel("p1000", "1000", "1a", "500", "1a Testby", '{"id":"p1000"}'),
    _parcel("p2000a", "2000", "2a", "500", "2a Testby", '{"id":"p2000a"}'),
    _parcel("p2000b", "2000", "2b", "500", "2b Testby", '{"id":"p2000b"}'),
    _parcel("p500", "500", "5a", "500", "5a Testby", '{"id":"p500"}'),
    _parcel("p6000a", "6000", "6a", "600", "Unicorn Kvarter", '{"id":"p6000a"}'),
    _parcel("p6000b", "6000", "6b", "600", "Unicorn Kvarter", '{"id":"p6000b"}'),
    _parcel("p7000", "7000", "7a", "700", "7a Skovby", '{"id":"p7000"}'),
]

_CHILD_BFES = [f"7{i:04d}" for i in range(150)]  # >100: exercises uncapped children
_CHILD_TYPES = [_UNIT] * 150

# EJENDOM_COLUMNS order: bfe, type, parent_bfe, ground_bfe, ejerlejlighedsnummer, jordstykke,
# geometry, chain_bfes, chain_types, children_bfes, children_types
_EJENDOM = [
    ("1000", _SFE, None, "1000", None, "p1000", None, ["1000"], [_SFE], ["3000"], [_UNIT]),
    (
        "2000",
        _SFE,
        None,
        "2000",
        None,
        "p2000a",
        '{"merged":"2000"}',
        ["2000"],
        [_SFE],
        ["4000"],
        [_BPFG],
    ),
    ("3000", _UNIT, "1000", "1000", "7", None, None, ["3000", "1000"], [_UNIT, _SFE], [], []),
    ("4000", _BPFG, "2000", "2000", None, None, None, ["4000", "2000"], [_BPFG, _SFE], [], []),
    ("5000", _BPFG, None, None, None, None, None, ["5000"], [_BPFG], [], []),
    ("500", _SFE, None, "500", None, "p500", None, ["500"], [_SFE], [], []),
    ("6000", _SFE, None, "6000", None, "p6000a", None, ["6000"], [_SFE], [], []),
    ("7000", _SFE, None, "7000", None, "p7000", None, ["7000"], [_SFE], _CHILD_BFES, _CHILD_TYPES),
]


@pytest.fixture
async def ejendom_source():
    schema = f"gen_test_{uuid4().hex}"
    pool = await asyncpg.create_pool(_DSN, server_settings={"search_path": f"{schema}, public"})
    async with pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(schema_sql())
        await conn.copy_records_to_table(
            "matrikel", records=_PARCELS, columns=MATRIKEL_COLUMNS[:-1]
        )
        await conn.copy_records_to_table("ejendom", records=_EJENDOM, columns=EJENDOM_COLUMNS[:-1])
    src = await PostgresAddressSource.from_pool(
        pool, generation=schema, seeded_at=datetime(2026, 8, 12, tzinfo=UTC)
    )
    async with src.snapshot() as snap:
        yield snap
    async with pool.acquire() as conn:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await pool.close()


def _by_bfe(hits):
    return {h.bfe: h for h in hits}


# ---- pure: strict-zip guard (no db) ----


def test_ejendom_geom_strict_zip_rejects_malformed_arrays():
    # parallel chain arrays out of sync is corruption -> fail loudly, never silently truncate
    rec = {
        "bfe": "1",
        "type": "samlet_fast_ejendom",
        "ejerlejlighedsnummer": None,
        "ground_bfe": "1",
        "chain_bfes": ["1"],
        "chain_types": ["samlet_fast_ejendom", "extra"],  # length mismatch
        "children_bfes": [],
        "children_types": [],
        "jordstykke": None,
        "matrikelnummer": None,
        "ejerlavskode": None,
        "ejerlavsnavn": None,
        "kommunekode": None,
        "kommunenavn": None,
        "centroid": None,
        "matrikelbetegnelse": None,
        "geometry": None,
    }
    with pytest.raises(ValueError):
        _ejendom_geom(rec, 1.0)


# ---- exact bfe across the three types ----


@_needs_db
async def test_by_code_exact_bfe_sfe(ejendom_source):
    hits = await ejendom_source.ejendom_by_code("1000", cap=5)
    assert len(hits) == 1
    h = hits[0]
    assert h.type is EjendomType.SAMLET_FAST_EJENDOM
    assert h.chain == (("1000", EjendomType.SAMLET_FAST_EJENDOM),)  # self only
    assert h.chain_complete is True and h.matrikelnummer == "1a"


@_needs_db
async def test_by_code_exact_bfe_unit_depth2_chain(ejendom_source):
    h = (await ejendom_source.ejendom_by_code("3000", cap=5))[0]
    assert h.type is EjendomType.EJERLEJLIGHED and h.ejerlejlighedsnummer == "7"
    assert [r.bfe for r in h.chain] == ["3000", "1000"]  # self -> ground
    assert h.chain[-1].type is EjendomType.SAMLET_FAST_EJENDOM
    assert h.chain_complete is True
    assert h.matrikelnummer == "1a"  # the ground sfe's representative parcel


@_needs_db
async def test_by_code_exact_bfe_bpfg_and_truncated_chain(ejendom_source):
    bpfg = (await ejendom_source.ejendom_by_code("4000", cap=5))[0]
    assert bpfg.type is EjendomType.BYGNING_PAA_FREMMED_GRUND
    assert [r.bfe for r in bpfg.chain] == ["4000", "2000"] and bpfg.chain_complete is True
    trunc = (await ejendom_source.ejendom_by_code("5000", cap=5))[0]
    assert trunc.chain == (("5000", EjendomType.BYGNING_PAA_FREMMED_GRUND),)
    assert trunc.chain_complete is False  # no ground -> dangling legal link
    assert trunc.matrikelbetegnelse is None and trunc.geometry is None  # no parcel


# ---- digit-branch dedup: bfe probe beats the ejerlav branch ----


@_needs_db
async def test_by_code_digit_dedup_probe_beats_ejerlav(ejendom_source):
    # "500" is both a bfe and an ejerlavskode; the probe row wins, the property appears once
    hits = await ejendom_source.ejendom_by_code("500", cap=10)
    bfes = [h.bfe for h in hits]
    assert bfes[0] == "500"  # probe first
    assert bfes.count("500") == 1  # not duplicated by the ejerlav branch
    assert {"1000", "2000"} <= set(bfes)  # the other in-ejerlav ground properties
    assert _by_bfe(hits)["500"].matrikelnummer == "5a"  # probe uses its representative parcel


# ---- betegnelse grouping: one hit per ground property ----


@_needs_db
async def test_by_betegnelse_groups_to_one_ground_property(ejendom_source):
    # bfe 6000 has two matching parcels; the result carries it once (best-sim parcel)
    hits = await ejendom_source.ejendom_by_betegnelse("unicorn kvarter", cap=10)
    assert [h.bfe for h in hits] == ["6000"]
    assert hits[0].type is EjendomType.SAMLET_FAST_EJENDOM
    assert hits[0].sim > 0.0


# ---- geometry fallback: multi-parcel merged vs single-parcel ----


@_needs_db
async def test_geometry_prefers_merged_ground_then_parcel(ejendom_source):
    single = (await ejendom_source.ejendom_by_code("1000", cap=5))[0]
    assert single.geometry == '{"id":"p1000"}'  # single-parcel sfe -> the parcel footprint
    multi = (await ejendom_source.ejendom_by_code("2000", cap=5))[0]
    assert multi.geometry == '{"merged":"2000"}'  # multi-parcel sfe -> the pre-merged footprint


# ---- contextual parcel selection (projection fan-out) ----


@_needs_db
async def test_by_bfes_context_parcel_overrides_representative(ejendom_source):
    with_ctx = await ejendom_source.ejendom_by_bfes([("4000", "p2000b")])
    assert with_ctx["4000"].matrikelnummer == "2b"  # the address's own parcel
    without = await ejendom_source.ejendom_by_bfes([("4000", None)])
    assert without["4000"].matrikelnummer == "2a"  # falls back to the ground representative
    assert with_ctx["4000"].geometry == '{"merged":"2000"}'  # geometry stays the ground footprint


# ---- limit + >100 children (uncapped) ----


@_needs_db
async def test_by_code_returns_all_children_uncapped(ejendom_source):
    h = (await ejendom_source.ejendom_by_code("7000", cap=5))[0]
    assert len(h.children) == 150  # uncapped: full child array served


@_needs_db
async def test_by_code_limit_counts_properties(ejendom_source):
    hits = await ejendom_source.ejendom_by_code("500", cap=1)
    assert len(hits) == 1 and hits[0].bfe == "500"  # limit counts properties, probe first
