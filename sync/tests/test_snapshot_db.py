"""snapshot build against a real postgres: tiny synthetic staging fixtures -> build_generation into
a gen_test_* schema -> assert the derived serving tables. dsn-gated (skip unset).

isolation: staging lands in a throwaway sync_test_<rand> schema (never the real "datafordeler"), the
generation carries a unique shape (invisible to serving + shape-scoped gc) with gc disabled, and
teardown drops the gen schema, the staging schema, and the public.generations row. never touches a
real seed.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
from bifrost.arms.normalize import normalize
from bifrost.db import generations
from bifrost.db.generations import Generation
from bifrost_sync.config import Config
from bifrost_sync.export import export_jsonl
from bifrost_sync.snapshot.build import _gc, build_generation
from bifrost_sync.snapshot.records import Floors

_DSN = os.environ.get("BIFROST_DATABASE_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="BIFROST_DATABASE_DSN unset")

# the ratio gates are off here (a ratio can't exceed 1.0): the 3-row fixtures deliberately model
# null bfe / no prior generation, which the calibrated production gates would refuse
_LOW = Floors(
    addresses=1,
    areas=1,
    matrikel=1,
    stednavne=1,
    ejendom=1,
    sfe=1,
    ejerlejlighed=1,
    bpfg=1,
    ebr_stamped=1,
    aux_postcode_dim=1,
    max_shrink=1.0,
    max_skipped=1.0,
    max_null=1.0,
    max_unmapped=1.0,
)


def _poly(*ring: tuple[float, float]) -> str:
    return json.dumps({"type": "Polygon", "coordinates": [[list(p) for p in ring]]})


def _line(*pts: tuple[float, float]) -> str:
    return json.dumps({"type": "LineString", "coordinates": [list(p) for p in pts]})


# text columns unless named here; every table also carries a dlt _deleted tombstone flag
_DOUBLE = {("dar_adressepunkt", "x"), ("dar_adressepunkt", "y")}

_LC = ("status", "virkningfra", "virkningtil")  # staged lifecycle cols (null -> classified current)
_HIST = ("status", "virkningfra", "virkningtil", "registreringfra", "registreringtil")

_TABLES: dict[str, tuple[str, ...]] = {
    "dar_navngivenvej": ("id", "vejnavn", "geometry", *_LC),
    "dar_navngivenvej_hist": ("id", "vejnavn", *_HIST),
    "dar_postnummer": ("id", "postnr", "navn"),
    "dar_postnummer_hist": ("id", "postnr", "navn", *_HIST),
    "dar_supplerendebynavn": ("id", "navn"),
    "dar_adressepunkt": ("id", "x", "y"),
    "dar_husnummer": (
        "id",
        "husnummertekst",
        "navngivenvej",
        "postnummer",
        "supplerende_bynavn",
        "adgangspunkt",
        "vejpunkt",
        "kommuneinddeling",
        "sogneinddeling",
        "jordstykke",
        *_LC,
    ),
    "dar_adresse": ("id", "husnummer", "etage", "door", *_LC),
    "dagi_kommuneinddeling": (
        "id",
        "navn",
        "code",
        "id_namespace",
        "geometry",
        "region_lokalid",
        "virkningfra",
        "virkningtil",
    ),
    "dagi_regionsinddeling": (
        "id",
        "navn",
        "code",
        "id_namespace",
        "geometry",
        "virkningfra",
        "virkningtil",
    ),
    "dagi_sogneinddeling": (
        "id",
        "navn",
        "code",
        "id_namespace",
        "geometry",
        "virkningfra",
        "virkningtil",
    ),
    "dagi_postnummerinddeling": (
        "id",
        "navn",
        "code",
        "id_namespace",
        "geometry",
        "virkningfra",
        "virkningtil",
    ),
    "dagi_retskreds": (
        "id",
        "navn",
        "code",
        "id_namespace",
        "geometry",
        "virkningfra",
        "virkningtil",
    ),
    "mat_jordstykke": (
        "id",
        "samletfastejendom_lokalid",
        "ejerlav_lokalid",
        "matrikelnummer",
        "kommunekode",
        *_LC,
    ),
    "mat_samletfastejendom": ("id", "bfe", *_LC),
    "mat_ejerlav": ("id", "ejerlavskode", "ejerlavsnavn"),
    "mat_centroide": ("jordstykke", "centroid"),
    "mat_lodflade": ("id", "jordstykke", "geometry"),
    "mat_ejerlejlighed": (
        "id",
        "bfe",
        "ejerlejlighedsnummer",
        "sfe_lokalid",
        "bpfg_punkt_lokalid",
        "bpfg_flade_lokalid",
        *_LC,
    ),
    "mat_bygningpaafremmedgrundpunkt": ("id", "bfe", "sfe_lokalid", *_LC),
    "mat_bygningpaafremmedgrundflade": ("id", "bfe", "sfe_lokalid", *_LC),
    "ebr_ejendomsbeliggenhed": ("id", "bfe", "adresse_lokalid"),
    "ds_stednavn": ("objectid", "skrivemaade", "navngivetsted_objectid", "aktualitet"),
    "ds_bebyggelse": ("objectid", "geometry"),
}


def _rows() -> dict[str, list[dict]]:
    return {
        "dar_navngivenvej": [
            {"id": "v1", "vejnavn": "Tejnvej", "geometry": _line((0, 0), (1, 1))},
            {"id": "v2", "vejnavn": "Ukendtvej", "geometry": _line((0, 0), (1, 1))},  # orphan road
        ],
        "dar_postnummer": [{"id": "pn1", "postnr": "3770", "navn": "Allinge"}],
        "dar_supplerendebynavn": [{"id": "sb1", "navn": "Sandkås"}],
        "dar_adressepunkt": [{"id": "ap1", "x": 869826.85, "y": 6138379.16}],
        "dar_husnummer": [
            {
                "id": "h1",
                "husnummertekst": "116H",
                "navngivenvej": "v1",
                "postnummer": "pn1",
                "supplerende_bynavn": "sb1",
                "adgangspunkt": "ap1",
                "vejpunkt": "ap1",
                "kommuneinddeling": "k1",
                "sogneinddeling": "sg1",
                "jordstykke": "j1",
            },
            # h2 has a postcode (road v2 gets one) but no adresse -> its street stays out the fact
            {"id": "h2", "husnummertekst": "5", "navngivenvej": "v2", "postnummer": "pn1"},
            # h3 on a multi-parcel sfe, no ebr unit -> ejendom_bfe falls back to the ground sfe
            {
                "id": "h3",
                "husnummertekst": "10",
                "navngivenvej": "v1",
                "postnummer": "pn1",
                "adgangspunkt": "ap1",
                "vejpunkt": "ap1",
                "kommuneinddeling": "k1",
                "sogneinddeling": "sg1",
                "jordstykke": "jm1",
            },
            # h4 has no jordstykke and no ebr unit -> ejendom_bfe stays null
            {
                "id": "h4",
                "husnummertekst": "12",
                "navngivenvej": "v1",
                "postnummer": "pn1",
                "adgangspunkt": "ap1",
                "vejpunkt": "ap1",
                "kommuneinddeling": "k1",
                "sogneinddeling": "sg1",
            },
        ],
        "dar_adresse": [
            {"id": "a1", "husnummer": "h1", "etage": "2", "door": "tv"},
            {"id": "a_del", "husnummer": "h1", "_deleted": True},  # tombstone -> not served
            {"id": "a2", "husnummer": "h3"},
            {"id": "a3", "husnummer": "h4"},
        ],
        "dagi_kommuneinddeling": [
            # same code at two scales: DISTINCT ON must keep the 1:500k (smaller) polygon
            {
                "id": "k1",
                "navn": "Bornholm",
                "code": "0400",
                "id_namespace": "http://data.gov.dk/dagi0500k",
                "geometry": _poly((0, 0), (1, 0), (1, 1), (0, 1), (0, 0)),
                "region_lokalid": "r1",
            },
            {
                "id": "k1full",
                "navn": "Bornholm",
                "code": "0400",
                "id_namespace": "http://data.gov.dk/dagi",
                "geometry": _poly((0, 0), (9, 0), (9, 9), (0, 9), (0, 0)),
                "region_lokalid": "r1",
            },
        ],
        "dagi_regionsinddeling": [
            {
                "id": "r1",
                "navn": "Hovedstaden",
                "code": "1084",
                "id_namespace": "http://data.gov.dk/dagi0500k",
                "geometry": _poly((0, 0), (1, 0), (1, 1), (0, 1), (0, 0)),
            }
        ],
        "dagi_sogneinddeling": [
            {
                "id": "sg1",
                "navn": "Olsker",
                "code": "7559",
                "id_namespace": "http://data.gov.dk/dagi0500k",
                "geometry": _poly((0, 0), (1, 0), (1, 1), (0, 1), (0, 0)),
            }
        ],
        "dagi_postnummerinddeling": [
            # two postnumre sharing a navn -> one unioned city polygon
            {
                "id": "pi1",
                "navn": "Allinge",
                "code": "3770",
                "id_namespace": "http://data.gov.dk/dagi0500k",
                "geometry": _poly((0, 0), (2, 0), (2, 2), (0, 2), (0, 0)),
            },
            {
                "id": "pi2",
                "navn": "Allinge",
                "code": "3760",
                "id_namespace": "http://data.gov.dk/dagi0500k",
                "geometry": _poly((2, 0), (4, 0), (4, 2), (2, 2), (2, 0)),
            },
        ],
        "dagi_retskreds": [
            {
                "id": "rk1",
                "navn": "Bornholms",
                "code": "16",
                "id_namespace": "http://data.gov.dk/dagi",
                "geometry": _poly(
                    (869000, 6138000),
                    (870000, 6138000),
                    (870000, 6139000),
                    (869000, 6139000),
                    (869000, 6138000),
                ),
            }
        ],
        "mat_jordstykke": [
            {
                "id": "j1",
                "samletfastejendom_lokalid": "sfe1",
                "ejerlav_lokalid": "el1",
                "matrikelnummer": "7000k",
                "kommunekode": "0400",
            },
            # j2: no lodflade -> geometry-less, skipped
            {"id": "j2", "samletfastejendom_lokalid": "sfe1", "matrikelnummer": "1a"},
            # j3: sfe has no bfe -> skipped
            {"id": "j3", "samletfastejendom_lokalid": "sfe2", "matrikelnummer": "2b"},
            # sfe_multi spans two parcels -> ejendom.geometry gets a merged MultiPolygon
            {
                "id": "jm1",
                "samletfastejendom_lokalid": "sfe_multi",
                "ejerlav_lokalid": "el1",
                "matrikelnummer": "1a",
                "kommunekode": "0400",
            },
            {
                "id": "jm2",
                "samletfastejendom_lokalid": "sfe_multi",
                "ejerlav_lokalid": "el1",
                "matrikelnummer": "1b",
                "kommunekode": "0400",
            },
        ],
        "mat_samletfastejendom": [
            {"id": "sfe1", "bfe": "100400001"},
            {"id": "sfe2", "bfe": None},
            {"id": "sfe_multi", "bfe": "200000001"},
        ],
        "mat_ejerlav": [{"id": "el1", "ejerlavskode": "100453", "ejerlavsnavn": "Tejn By, Olsker"}],
        "mat_centroide": [{"jordstykke": "j1", "centroid": "869800 6138300"}],
        "mat_lodflade": [
            # two lodflader for j1 -> merged MultiPolygon
            {"id": "l1", "jordstykke": "j1", "geometry": _poly((0, 0), (1, 0), (1, 1), (0, 0))},
            {"id": "l2", "jordstykke": "j1", "geometry": _poly((2, 2), (3, 2), (3, 3), (2, 2))},
            {"id": "l3", "jordstykke": "j3", "geometry": _poly((4, 4), (5, 4), (5, 5), (4, 4))},
            # one lodflade per multi-parcel parcel; both feed the sfe_multi merge
            {"id": "lm1", "jordstykke": "jm1", "geometry": _poly((0, 0), (1, 0), (1, 1), (0, 0))},
            {"id": "lm2", "jordstykke": "jm2", "geometry": _poly((5, 5), (6, 5), (6, 6), (5, 5))},
        ],
        # depth-3 (unit->bpfg->sfe), depth-2 (unit->sfe), bpfg-first precedence, dual-ref punkt-wins
        "mat_bygningpaafremmedgrundpunkt": [
            {"id": "bpfg1", "bfe": "300000001", "sfe_lokalid": "sfe1"},
        ],
        "mat_bygningpaafremmedgrundflade": [
            {"id": "bpfg_flade1", "bfe": "300000002", "sfe_lokalid": "sfe1"},
        ],
        "mat_ejerlejlighed": [
            {
                "id": "unit_deep",
                "bfe": "400000001",
                "ejerlejlighedsnummer": "4",
                "bpfg_punkt_lokalid": "bpfg1",
            },
            {
                "id": "unit_sfe",
                "bfe": "400000002",
                "ejerlejlighedsnummer": "1",
                "sfe_lokalid": "sfe1",
            },
            # both sfe + bpfg refs populated: bpfg wins, ground climbs to the bpfg's own sfe
            {
                "id": "unit_both",
                "bfe": "400000003",
                "ejerlejlighedsnummer": "2",
                "sfe_lokalid": "sfe_multi",
                "bpfg_punkt_lokalid": "bpfg1",
            },
            # both bpfg pointers populated: punkt wins over flade
            {
                "id": "unit_dual",
                "bfe": "400000004",
                "ejerlejlighedsnummer": "3",
                "bpfg_punkt_lokalid": "bpfg1",
                "bpfg_flade_lokalid": "bpfg_flade1",
            },
        ],
        # a1 carries an invalid (sfe, smaller bfe) ebr row + a valid ejerlejlighed one; the valid
        # unit wins despite the smaller sibling -> membership validated before the min() dedup
        "ebr_ejendomsbeliggenhed": [
            {"id": "eb1", "bfe": "100400001", "adresse_lokalid": "a1"},
            {"id": "eb2", "bfe": "400000002", "adresse_lokalid": "a1"},
        ],
        "ds_stednavn": [
            {"objectid": "st1", "skrivemaade": "Allinge", "navngivetsted_objectid": "place1"},
            {"objectid": "st2", "skrivemaade": "Sandkås", "navngivetsted_objectid": "place1"},
        ],
        "ds_bebyggelse": [
            {"objectid": "place1", "geometry": _poly((0, 0), (1, 0), (1, 1), (0, 0))}
        ],
        "dar_navngivenvej_hist": [],  # base fixture: no name history (empty is valid, guarded)
        "dar_postnummer_hist": [],
    }


async def _seed_staging(
    conn: asyncpg.Connection, staging: str, rows: dict[str, list[dict]] | None = None
) -> None:
    rows = _rows() if rows is None else rows
    await conn.execute(f'CREATE SCHEMA "{staging}"')
    for table, cols in _TABLES.items():
        defs = ", ".join(
            f"{c} double precision" if (table, c) in _DOUBLE else f"{c} text" for c in cols
        )
        await conn.execute(f'CREATE TABLE "{staging}"."{table}" ({defs}, _deleted boolean)')
    for table, cols in _TABLES.items():
        for row in rows.get(table, []):
            allcols = (*cols, "_deleted")
            ph = ", ".join(f"${i + 1}" for i in range(len(allcols)))
            await conn.execute(
                f'INSERT INTO "{staging}"."{table}" ({", ".join(allcols)}) VALUES ({ph})',
                *(row.get(c) for c in allcols),
            )


class _Env:
    def __init__(self, conn: asyncpg.Connection):
        self.conn = conn
        self.staging = f"sync_test_{uuid4().hex}"
        self.gens: list[str] = []  # gen schemas to drop (+ their generations rows)

    def gen_name(self) -> str:
        name = f"gen_test_{uuid4().hex}"
        self.gens.append(name)
        return name

    async def drop(self) -> None:
        for g in self.gens:
            await self.conn.execute(f'DROP SCHEMA IF EXISTS "{g}" CASCADE')
            await self.conn.execute("DELETE FROM public.generations WHERE schema_name = $1", g)
        await self.conn.execute(f'DROP SCHEMA IF EXISTS "{self.staging}" CASCADE')


@pytest.fixture
async def env() -> Iterator[_Env]:
    conn = await asyncpg.connect(_DSN)
    e = _Env(conn)
    try:
        await _seed_staging(conn, e.staging)
        yield e
    finally:
        await e.drop()
        await conn.close()


async def _query(schema: str, method: str, sql: str, *args: object):
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute(f'SET search_path TO "{schema}", public')
        return await getattr(conn, method)(sql, *args)
    finally:
        await conn.close()


async def _fetchval(schema: str, sql: str, *args: object):
    return await _query(schema, "fetchval", sql, *args)


async def _fetchrow(schema: str, sql: str, *args: object):
    return await _query(schema, "fetchrow", sql, *args)


async def _fetch(schema: str, sql: str, *args: object):
    return await _query(schema, "fetch", sql, *args)


@_needs_db
async def test_build_generation_derives_all_serving_tables(env: _Env):
    cfg = Config(dsn=_DSN, work_dir=os.getcwd())
    schema = env.gen_name()
    shape = f"test-{uuid4().hex}"
    result = await build_generation(
        cfg,
        cursors={"dar_adresse": 42},
        floors=_LOW,
        staging=env.staging,
        schema=schema,
        shape=shape,
        gc=False,
    )
    assert result == schema

    # addresses: the tombstoned adresse is excluded; codes come from the dagi + PIP joins
    assert await _fetchval(schema, "SELECT count(*) FROM addresses") == 3
    addr = await _fetchrow(schema, "SELECT * FROM addresses WHERE address_id = 'a1'")
    assert addr["house_number"] == "116" and addr["house_letter"] == "H"
    assert addr["kommunekode"] == "0400"  # via husnummer kommuneinddeling ref
    assert addr["regionskode"] == "1084"  # chained off the kommune row's region_lokalid
    assert addr["sognekode"] == "7559"
    assert addr["retskredsnummer"] == "16"  # stamped by point-in-polygon
    assert addr["jordstykke"] == "j1"  # kept: gen.matrikel holds the parcel (currency gate)

    # matrikel: j1 + the two sfe_multi parcels (j2 geometry-less, j3 bfe-less)
    assert await _fetchval(schema, "SELECT count(*) FROM matrikel") == 3
    mat = await _fetchrow(schema, "SELECT * FROM matrikel WHERE jordstykke = 'j1'")
    assert mat["bfe"] == "100400001"
    assert json.loads(mat["geometry"])["type"] == "MultiPolygon"  # two lodflader concatenated
    assert mat["kommunenavn"] == "Bornholm"  # resolved from dagi by code
    assert mat["ejerlavskode"] == "100453"
    assert mat["matrikelbetegnelse"] == "7000k Tejn By, Olsker"

    # matrikel secondaries dropped for the bulk copy then recreated: 3 defs + the pkey
    mat_indexes = {
        r["indexname"]
        for r in await env.conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE schemaname = $1 AND tablename = 'matrikel'",
            schema,
        )
    }
    assert mat_indexes == {
        "matrikel_betegnelse_trgm",
        "matrikel_bfe",
        "matrikel_ejerlavskode",
        "matrikel_pkey",
    }

    # ejendom: 2 sfe + 4 ejerlejligheder + 2 bpfg, one row per bfe
    assert await _fetchval(schema, "SELECT count(*) FROM ejendom") == 8
    by_type = {
        r["type"]: r["n"]
        for r in await _fetch(schema, "SELECT type, count(*) AS n FROM ejendom GROUP BY type")
    }
    assert by_type == {"samlet_fast_ejendom": 2, "ejerlejlighed": 4, "bygning_paa_fremmed_grund": 2}

    # depth-3 chain (unit -> bpfg -> ground sfe); chain_complete iff ground_bfe present
    deep = await _fetchrow(schema, "SELECT * FROM ejendom WHERE bfe = '400000001'")
    assert deep["chain_bfes"] == ["400000001", "300000001", "100400001"]
    assert deep["chain_types"] == [
        "ejerlejlighed",
        "bygning_paa_fremmed_grund",
        "samlet_fast_ejendom",
    ]
    assert deep["parent_bfe"] == "300000001" and deep["ground_bfe"] == "100400001"
    assert deep["ejerlejlighedsnummer"] == "4"

    # depth-2 chain (unit directly in an sfe)
    direct = await _fetchrow(schema, "SELECT * FROM ejendom WHERE bfe = '400000002'")
    assert direct["chain_bfes"] == ["400000002", "100400001"]
    assert direct["parent_bfe"] == "100400001" and direct["ground_bfe"] == "100400001"

    # bpfg-first precedence: sfe + bpfg refs both set -> parent is the bpfg, ground its sfe
    both = await _fetchrow(
        schema, "SELECT parent_bfe, ground_bfe FROM ejendom WHERE bfe='400000003'"
    )
    assert both["parent_bfe"] == "300000001" and both["ground_bfe"] == "100400001"

    # dual bpfg refs: punkt wins over flade
    dual = await _fetchval(schema, "SELECT parent_bfe FROM ejendom WHERE bfe = '400000004'")
    assert dual == "300000001"

    # children invert parent_bfe, uncapped + bfe-sorted
    sfe1 = await _fetchrow(schema, "SELECT * FROM ejendom WHERE bfe = '100400001'")
    assert sfe1["children_bfes"] == ["300000001", "300000002", "400000002"]
    assert sfe1["children_types"] == [
        "bygning_paa_fremmed_grund",
        "bygning_paa_fremmed_grund",
        "ejerlejlighed",
    ]
    bpfg1 = await _fetchrow(schema, "SELECT children_bfes FROM ejendom WHERE bfe = '300000001'")
    assert bpfg1["children_bfes"] == ["400000001", "400000003", "400000004"]

    # geometry: multi-parcel sfe pre-merged, representative parcel = min(jordstykke)
    multi = await _fetchrow(
        schema, "SELECT geometry, jordstykke FROM ejendom WHERE bfe='200000001'"
    )
    assert json.loads(multi["geometry"])["type"] == "MultiPolygon"
    assert multi["jordstykke"] == "jm1"
    # single-parcel sfe: geometry served from matrikel, not stored on ejendom
    assert sfe1["geometry"] is None and sfe1["jordstykke"] == "j1"

    # addresses.ejendom_bfe: unit stamp (valid ejerlejlighed beats the smaller invalid sfe row),
    # sfe fallback, and null when neither a unit nor a parcel resolves
    assert addr["ejendom_bfe"] == "400000002"
    a2 = await _fetchval(schema, "SELECT ejendom_bfe FROM addresses WHERE address_id = 'a2'")
    assert a2 == "200000001"  # no ebr unit -> ground sfe of the parcel
    a3 = await _fetchval(schema, "SELECT ejendom_bfe FROM addresses WHERE address_id = 'a3'")
    assert a3 is None  # no jordstykke, no ebr unit

    # areas: kommune picked at 1:500k, plus the synthesized city union
    kom = await _fetchval(
        schema, "SELECT geometry FROM admin_area WHERE kind = 'kommune' AND code = '0400'"
    )
    assert json.loads(kom)["coordinates"][0][2] == [1, 1]  # the small 1:500k poly, not the 9x9
    city = await _fetchval(schema, "SELECT geometry FROM admin_area WHERE area_id = 'city:Allinge'")
    assert json.loads(city)["type"] in ("Polygon", "MultiPolygon")  # two postnumre unioned
    assert await _fetchval(schema, "SELECT count(*) FROM admin_area WHERE kind = 'city'") == 1
    # politikreds/opstillingskreds tables absent -> tolerated, no such rows
    assert (
        await _fetchval(schema, "SELECT count(*) FROM admin_area WHERE kind = 'politikreds'") == 0
    )

    # roads: v1 kept (has an address), v2 dropped (orphan street)
    assert await _fetchval(schema, "SELECT count(*) FROM road") == 1
    road = await _fetchrow(schema, "SELECT * FROM road WHERE navngivenvej_id = 'v1'")
    assert road["postcodes"] == ["3770"]
    assert road["street_id"] == addr["street_id"]  # same collapsed street as the address

    # stednavne: both the primary name and its alias, tagged with the bebyggelse wire type
    assert await _fetchval(schema, "SELECT count(*) FROM stednavne") == 2
    assert (
        await _fetchval(schema, "SELECT type FROM stednavne WHERE stednavn_id = 'st1'")
        == "bebyggelse"
    )

    # aux: accrued over the address stream and written into the gen tables (parity via normalize)
    dim = await _fetch(schema, "SELECT postcode FROM aux_postcode_dim")
    assert {r["postcode"] for r in dim} == {"3770"}
    assert (
        await _fetchval(
            schema, "SELECT postcode FROM aux_city_map WHERE folded_name = $1", normalize("Allinge")
        )
        == "3770"
    )
    assert (
        await _fetchval(
            schema,
            "SELECT postcode FROM aux_subloc_map WHERE folded_name = $1",
            normalize("Sandkås"),
        )
        == "3770"
    )

    # register + watermark landed
    row = await env.conn.fetchrow(
        "SELECT shape, row_count, matrikel_count, ejendom_count "
        "FROM public.generations WHERE schema_name = $1",
        schema,
    )
    assert row["shape"] == shape and row["row_count"] == 3 and row["matrikel_count"] == 3
    assert row["ejendom_count"] == 8
    wm = await env.conn.fetchval(
        f"SELECT value FROM \"{env.staging}\".sync_meta WHERE key = 'watermark'"
    )
    # cursor + contract stamped after register; no contract passed -> null
    assert json.loads(wm) == {"dar_adresse": {"gen": 42, "contract": None}}


@_needs_db
async def test_floor_violation_aborts_before_register(env: _Env):
    cfg = Config(dsn=_DSN, work_dir=os.getcwd())
    schema = env.gen_name()
    with pytest.raises(SystemExit):
        await build_generation(
            cfg,
            cursors={},
            floors=Floors(),  # default 3.5M address floor: the 1-row fixture is short
            staging=env.staging,
            schema=schema,
            shape=f"test-{uuid4().hex}",
            gc=False,
        )
    # no registered row; the partial gen schema is dropped in teardown
    assert (
        await env.conn.fetchval(
            "SELECT count(*) FROM public.generations WHERE schema_name = $1", schema
        )
        == 0
    )


@_needs_db
async def test_column_coverage_violation_aborts_before_register(env: _Env):
    # h4 carries no jordstykke and no ebr unit -> a null ejendom_bfe; with no null budget the
    # coverage gate must refuse the generation exactly like a count floor does
    cfg = Config(dsn=_DSN, work_dir=os.getcwd())
    schema = env.gen_name()
    with pytest.raises(SystemExit, match="ejendom_bfe"):
        await build_generation(
            cfg,
            cursors={},
            floors=replace(_LOW, max_null=0.0),
            staging=env.staging,
            schema=schema,
            shape=f"test-{uuid4().hex}",
            gc=False,
        )
    assert (
        await env.conn.fetchval(
            "SELECT count(*) FROM public.generations WHERE schema_name = $1", schema
        )
        == 0
    )


@_needs_db
async def test_lane_failure_aborts_build(env: _Env, monkeypatch):
    # a stage-1 lane loader raising cancels the sibling lanes and surfaces as an ExceptionGroup out
    # of the barrier; nothing registers, and the unregistered gen schema is dropped in teardown
    cfg = Config(dsn=_DSN, work_dir=os.getcwd())
    schema = env.gen_name()

    async def _boom(*args, **kwargs):
        raise RuntimeError("lane boom")

    monkeypatch.setattr("bifrost_sync.snapshot.build.load_areas", _boom)

    with pytest.raises(ExceptionGroup) as excinfo:
        await build_generation(
            cfg,
            cursors={},
            floors=_LOW,
            staging=env.staging,
            schema=schema,
            shape=f"test-{uuid4().hex}",
            gc=False,
        )
    assert excinfo.group_contains(RuntimeError, match="lane boom")
    assert (
        await env.conn.fetchval(
            "SELECT count(*) FROM public.generations WHERE schema_name = $1", schema
        )
        == 0
    )


@_needs_db
async def test_gc_drops_targeted_generation_and_is_idempotent(env: _Env, monkeypatch):
    # patch gc_targets to this fixture's schema only: unpatched gc scans every gen_* schema + all of
    # public.generations and would drop live dev generations
    schema = env.gen_name()
    shape = f"test-{uuid4().hex}"
    await env.conn.execute(f'CREATE SCHEMA "{schema}"')
    gen = Generation(schema, shape, 10, 1, 1, 1, 1, datetime.now(UTC))
    await generations.register(env.conn, gen)

    monkeypatch.setattr(generations, "gc_targets", lambda *a, **k: [schema])
    await _gc(env.conn)
    assert (
        await env.conn.fetchval("SELECT count(*) FROM pg_namespace WHERE nspname = $1", schema) == 0
    )
    assert (
        await env.conn.fetchval(
            "SELECT count(*) FROM public.generations WHERE schema_name = $1", schema
        )
        == 0
    )

    # same target again: DROP IF EXISTS + DELETE of nothing -> no-op, no error
    await _gc(env.conn)


@_needs_db
async def test_export_jsonl_emits_the_corpus_record(env: _Env, tmp_path):
    # export builds its own transient sync_export_* scaffold (matrikel + PIP stamp) and drops it
    cfg = Config(dsn=_DSN, work_dir=os.getcwd())
    out = tmp_path / "baseline.jsonl"
    assert await export_jsonl(cfg, out, staging=env.staging) == 3
    recs = {json.loads(line)["id"]: json.loads(line) for line in out.read_text().splitlines()}
    assert recs["a1"] == {
        "id": "a1",
        "street_name": "Tejnvej",
        "house_number": "116",
        "house_letter": "H",
        "floor": "2",
        "door": "tv",
        "sub_locality": "Sandkås",
        "postcode": "3770",
        "city": "Allinge",
        "adgangspunkt_x": 869826.85,
        "adgangspunkt_y": 6138379.16,
        "vejpunkt_x": 869826.85,
        "vejpunkt_y": 6138379.16,
        "kommunekode": "0400",
        "regionskode": "1084",
        "sognekode": "7559",
        "retskredsnummer": "16",  # point-in-polygon stamped
        "politikredsnummer": None,
        "opstillingskredsnummer": None,
        "jordstykke": "j1",  # kept: the matrikel scaffold holds the parcel
        "ejendom_bfe": "100400001",  # ejendom empty in the export scaffold -> ground sfe fallback
    }


@_needs_db
async def test_export_jsonl_floor_aborts_short(env: _Env, tmp_path):
    cfg = Config(dsn=_DSN, work_dir=os.getcwd())
    with pytest.raises(SystemExit):
        await export_jsonl(cfg, tmp_path / "short.jsonl", min_rows=10, staging=env.staging)


# lifecycle features: rename -> street_alias + point-in-time, Historisk jordstykke retained in
# matrikel + null geometry, preliminary rows, area_alias, export/aux current-only

_PAST = "2000-01-01T00:00:00Z"
_MID = "2015-01-01T00:00:00Z"
_RET_INSTANT = "2010-06-01T00:00:00Z"  # inside the old-name window [_PAST, _MID)


def _hist(id_: str, name_col: str, name: str, vfra: str, vtil: str | None) -> dict:
    return {
        "id": id_,
        name_col: name,
        "status": "3",
        "virkningfra": vfra,
        "virkningtil": vtil,
        "registreringfra": vfra,
        "registreringtil": None,
    }


def _lifecycle_rows() -> dict[str, list[dict]]:
    rows = _rows()
    # v1 renamed Gammelvej -> Tejnvej at _MID; pn1 renamed Gammel By -> Allinge
    rows["dar_navngivenvej_hist"] = [
        {**_hist("v1", "vejnavn", "Gammelvej", _PAST, _MID)},
        {**_hist("v1", "vejnavn", "Tejnvej", _MID, None)},
    ]
    rows["dar_postnummer_hist"] = [{**_hist("pn1", "navn", "Gammel By", _PAST, _MID)}]
    rows["dar_husnummer"] = rows["dar_husnummer"] + [
        {
            "id": "h_ret",
            "husnummertekst": "9",
            "navngivenvej": "v1",
            "postnummer": "pn1",
            "adgangspunkt": "ap1",
            "vejpunkt": "ap1",
            "kommuneinddeling": "k1",
            "sogneinddeling": "sg1",
            "status": "4",
            "virkningfra": _RET_INSTANT,
        },
        {
            "id": "h_prelim",
            "husnummertekst": "11",
            "navngivenvej": "v1",
            "postnummer": "pn1",
            "adgangspunkt": "ap1",
            "kommuneinddeling": "k1",
            "sogneinddeling": "sg1",
            "status": "2",
        },
    ]
    rows["dar_adresse"] = rows["dar_adresse"] + [
        {"id": "a_ret", "husnummer": "h_ret", "status": "4", "virkningfra": _RET_INSTANT},
        {"id": "a_prelim", "husnummer": "h_prelim", "status": "2"},
    ]
    # a Historisk parcel with NO lodflade (null geometry) on its own retired sfe, kept in matrikel
    rows["mat_jordstykke"] = rows["mat_jordstykke"] + [
        {
            "id": "j_ret",
            "samletfastejendom_lokalid": "sfe_ret",
            "ejerlav_lokalid": "el1",
            "matrikelnummer": "99z",
            "kommunekode": "0400",
            "status": "Historisk",
        },
    ]
    rows["mat_samletfastejendom"] = rows["mat_samletfastejendom"] + [
        {"id": "sfe_ret", "bfe": "900000001", "status": "Historisk"},
    ]
    return rows


@_needs_db
async def test_lifecycle_features(env: _Env):
    cfg = Config(dsn=_DSN, work_dir=os.getcwd())
    staging = f"sync_test_{uuid4().hex}"
    await _seed_staging(env.conn, staging, _lifecycle_rows())
    schema = env.gen_name()
    try:
        await build_generation(
            cfg,
            cursors={},
            floors=_LOW,
            staging=staging,
            schema=schema,
            shape=f"test-{uuid4().hex}",
            gc=False,
        )

        # retired address renders point-in-time names; a current one keeps the current name
        ret = await _fetchrow(
            schema,
            "SELECT a.lifecycle, a.city, d.street FROM addresses a "
            "JOIN street_dim d ON d.street_id = a.street_id WHERE a.address_id = 'a_ret'",
        )
        assert ret["lifecycle"] == "retired"
        assert ret["street"] == "Gammelvej"  # point-in-time street name at the retirement instant
        assert ret["city"] == "Gammel By"  # point-in-time city name
        cur = await _fetchrow(
            schema,
            "SELECT a.lifecycle, d.street FROM addresses a "
            "JOIN street_dim d ON d.street_id = a.street_id WHERE a.address_id = 'a1'",
        )
        assert cur["lifecycle"] == "current" and cur["street"] == "Tejnvej"
        assert (
            await _fetchval(schema, "SELECT lifecycle FROM addresses WHERE address_id = 'a_prelim'")
            == "preliminary"
        )

        # street_alias: the prior name -> the current road's street_id, postcodes scoped
        alias = await _fetchrow(
            schema, "SELECT * FROM street_alias WHERE folded_street = $1", normalize("Gammelvej")
        )
        assert alias["lifecycle"] == "retired" and alias["postcodes"] == ["3770"]
        tejn = await _fetchval(schema, "SELECT street_id FROM road WHERE navngivenvej_id = 'v1'")
        assert alias["street_id"] == tejn  # points at the current Tejnvej road, not a new id

        # area_alias: the prior postdistrikt name -> its postcode admin_area
        area = await _fetchrow(
            schema, "SELECT * FROM area_alias WHERE folded_name = $1", normalize("Gammel By")
        )
        assert area["lifecycle"] == "retired"
        assert area["area_id"] == await _fetchval(
            schema, "SELECT area_id FROM admin_area WHERE kind = 'postcode' AND code = '3770'"
        )

        # Historisk jordstykke: kept in matrikel with null geometry for the history betegnelse KNN
        jret = await _fetchrow(
            schema,
            "SELECT lifecycle, geometry, bfe, folded_betegnelse FROM matrikel "
            "WHERE jordstykke = 'j_ret'",
        )
        assert jret["lifecycle"] == "retired" and jret["geometry"] is None
        assert jret["bfe"] == "900000001" and normalize("99z") in jret["folded_betegnelse"]
        assert (
            await _fetchval(schema, "SELECT lifecycle FROM ejendom WHERE bfe = '900000001'")
            == "retired"
        )
    finally:
        await env.conn.execute(f'DROP SCHEMA IF EXISTS "{staging}" CASCADE')


@_needs_db
async def test_export_and_aux_are_current_only(env: _Env, tmp_path):
    # export drops non-current rows; aux accrues over current addresses only
    cfg = Config(dsn=_DSN, work_dir=os.getcwd())
    staging = f"sync_test_{uuid4().hex}"
    await _seed_staging(env.conn, staging, _lifecycle_rows())
    schema = env.gen_name()
    try:
        out = tmp_path / "corpus.jsonl"
        n = await export_jsonl(cfg, out, staging=staging)
        ids = {json.loads(line)["id"] for line in out.read_text().splitlines()}
        assert "a_ret" not in ids and "a_prelim" not in ids  # non-current excluded
        assert n == len(ids)
        assert "lifecycle" not in json.loads(out.read_text().splitlines()[0])  # corpus record shape

        await build_generation(
            cfg,
            cursors={},
            floors=_LOW,
            staging=staging,
            schema=schema,
            shape=f"test-{uuid4().hex}",
            gc=False,
        )
        # aux city map has the current postdistrikt name, never the retired one
        cities = await _fetch(schema, "SELECT folded_name FROM aux_city_map")
        folded = {r["folded_name"] for r in cities}
        assert normalize("Allinge") in folded and normalize("Gammel By") not in folded
    finally:
        await env.conn.execute(f'DROP SCHEMA IF EXISTS "{staging}" CASCADE')
