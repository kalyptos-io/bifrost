"""dlt staging pipeline against a real postgres: baseline load, delta merge w/ hard_delete, and
refresh re-baseline. dsn-gated (skip unset); every test lands in a throwaway sync_test_<rand>
dataset dropped in teardown, so the real "datafordeler" dataset is never touched.
"""

from __future__ import annotations

import csv
import io
import json
import os
import zipfile
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from bifrost_sync.config import Config
from bifrost_sync.pipeline import Load, contracts, cursors, make_pipeline, run_loads
from bifrost_sync.reduce import baseline_rows, reduce_delta_files
from bifrost_sync.registers import ALL_ENTITIES, EntitySpec, contract_hash

_DSN = os.environ.get("BIFROST_DATABASE_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="BIFROST_DATABASE_DSN unset")

_OPEN = {"registreringTil": "", "virkningTil": ""}
_CLOSED = {"registreringTil": "2026-07-09T00:00:00Z", "virkningTil": ""}


def _spec(table: str) -> EntitySpec:
    return next(s for s in ALL_ENTITIES if s.table == table)


def _zip(path: Path, rows: list[dict]) -> Path:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("data.csv", buf.getvalue().encode("utf-8"))
    return path


class _Env:
    """a per-test dlt pipeline into an isolated dataset, plus pg asserts; drops schemas on close."""

    def __init__(self, cfg: Config, conn: asyncpg.Connection):
        self._cfg = cfg
        self.conn = conn
        self.dataset = f"sync_test_{uuid4().hex}"

    def pipeline(self):
        # unique pipeline_name so dlt state can't collide with the real "datafordeler" pipeline
        return make_pipeline(self._cfg, dataset=self.dataset, pipeline_name=self.dataset)

    async def rows(self, table: str) -> list[dict]:
        recs = await self.conn.fetch(f'SELECT * FROM "{self.dataset}"."{table}"')
        return [dict(r) for r in recs]

    async def types(self, table: str) -> dict[str, str]:
        recs = await self.conn.fetch(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = $2",
            self.dataset,
            table,
        )
        return {r["column_name"]: r["data_type"] for r in recs}

    async def drop(self) -> None:
        await self.conn.execute(f'DROP SCHEMA IF EXISTS "{self.dataset}" CASCADE')
        await self.conn.execute(f'DROP SCHEMA IF EXISTS "{self.dataset}_staging" CASCADE')


@pytest.fixture
async def env(tmp_path) -> Iterator[_Env]:
    cfg = Config(dsn=_DSN, work_dir=tmp_path)
    conn = await asyncpg.connect(_DSN)
    e = _Env(cfg, conn)
    try:
        yield e
    finally:
        await e.drop()
        await conn.close()


@_needs_db
async def test_baseline_stages_rows_and_types(env: _Env):
    pn, ap, nv = _spec("dar_postnummer"), _spec("dar_adressepunkt"), _spec("dar_navngivenvej")
    pn_zip = _zip(
        env._cfg.work_dir / "pn.zip",
        [
            {"id_lokalId": "p1", "postnr": "0800", "navn": "Høje"},
            {"id_lokalId": "p2", "postnr": "4850", "navn": "Stubbe"},
        ],
    )
    ap_zip = _zip(
        env._cfg.work_dir / "ap.zip",
        [{"id_lokalId": "ap1", "position": "POINT(722345.67 6179535.68)"}],
    )
    nv_zip = _zip(
        env._cfg.work_dir / "nv.zip",
        [{"id_lokalId": "v1", "vejnavn": "Testvej", "geom": "LINESTRING(0 0,1 1)"}],
    )
    p = env.pipeline()
    run_loads(
        p,
        [
            Load(pn, baseline_rows([pn_zip], pn), 100),
            Load(ap, baseline_rows([ap_zip], ap), 100),
            Load(nv, baseline_rows([nv_zip], nv), 100),
        ],
        refresh="drop_resources",
    )

    pn_rows = {r["id"]: r for r in await env.rows("dar_postnummer")}
    assert set(pn_rows) == {"p1", "p2"}
    assert pn_rows["p1"]["postnr"] == "0800"  # leading zero survives (text, not int)

    types = await env.types("dar_postnummer")
    assert types["postnr"] == "character varying"
    assert types["_deleted"] == "boolean"
    ap_types = await env.types("dar_adressepunkt")
    assert ap_types["x"] == "double precision" and ap_types["y"] == "double precision"

    nv_rows = await env.rows("dar_navngivenvej")
    assert json.loads(nv_rows[0]["geometry"]) == {
        "type": "LineString",
        "coordinates": [[0, 0], [1, 1]],
    }
    assert (await env.types("dar_navngivenvej"))["geometry"] == "character varying"

    assert cursors(p) == {"dar_postnummer": 100, "dar_adressepunkt": 100, "dar_navngivenvej": 100}


@_needs_db
async def test_delta_upsert_and_tombstone(env: _Env):
    pn = _spec("dar_postnummer")

    def _pn(id_: str, navn: str, meta: dict) -> dict:
        return {"id_lokalId": id_, "postnr": "4850", "navn": navn, "status": "3", **meta}

    base = _zip(env._cfg.work_dir / "b.zip", [_pn("p1", "Old", _OPEN), _pn("p2", "Doomed", _OPEN)])
    p = env.pipeline()
    run_loads(p, [Load(pn, baseline_rows([base], pn), 100)], refresh="drop_resources")

    delta = _zip(
        env._cfg.work_dir / "d.zip",
        [_pn("p1", "New", _CLOSED), _pn("p1", "New", _OPEN), _pn("p2", "Doomed", _CLOSED)],
    )
    run_loads(p, [Load(pn, reduce_delta_files([(103, delta)], pn), 103)])

    rows = {r["id"]: r for r in await env.rows("dar_postnummer")}
    assert set(rows) == {"p1"}  # p2 tombstone hard-deleted
    assert rows["p1"]["navn"] == "New"  # p1 upserted to the current-effective row
    assert cursors(p)["dar_postnummer"] == 103


@_needs_db
async def test_refresh_rebaseline_resets_table_and_cursor(env: _Env):
    pn = _spec("dar_postnummer")
    b1 = _zip(
        env._cfg.work_dir / "b1.zip",
        [
            {"id_lokalId": "p1", "postnr": "1000", "navn": "A"},
            {"id_lokalId": "p2", "postnr": "2000", "navn": "B"},
        ],
    )
    p = env.pipeline()
    run_loads(p, [Load(pn, baseline_rows([b1], pn), 100)], refresh="drop_resources")
    d = _zip(
        env._cfg.work_dir / "d.zip", [{"id_lokalId": "p3", "postnr": "3000", "navn": "C", **_OPEN}]
    )
    run_loads(p, [Load(pn, reduce_delta_files([(103, d)], pn), 103)])
    assert cursors(p)["dar_postnummer"] == 103

    b2 = _zip(env._cfg.work_dir / "b2.zip", [{"id_lokalId": "p9", "postnr": "9000", "navn": "Z"}])
    run_loads(p, [Load(pn, baseline_rows([b2], pn), 200)], refresh="drop_resources")

    rows = {r["id"] for r in await env.rows("dar_postnummer")}
    assert rows == {"p9"}  # refresh dropped the table; only the new baseline survives
    assert cursors(p)["dar_postnummer"] == 200  # cursor reset from 103 to the new baseline


@_needs_db
async def test_contract_state_survives_destination_restoration(env: _Env):
    pn = _spec("dar_postnummer")
    z = _zip(env._cfg.work_dir / "b.zip", [{"id_lokalId": "p1", "postnr": "1000", "navn": "A"}])
    p = env.pipeline()
    run_loads(p, [Load(pn, baseline_rows([z], pn), 100)], refresh="drop_resources")
    assert contracts(p)["dar_postnummer"] == contract_hash(pn)  # committed with the load

    # a fresh pipeline object (no in-memory state) restores both cursor + contract from the dest
    restored = env.pipeline()
    restored.sync_destination()
    assert cursors(restored)["dar_postnummer"] == 100
    assert contracts(restored)["dar_postnummer"] == contract_hash(pn)


@_needs_db
async def test_drop_resources_replaces_only_the_affected_resource(env: _Env):
    pn, sb = _spec("dar_postnummer"), _spec("dar_supplerendebynavn")
    pz = _zip(env._cfg.work_dir / "pn.zip", [{"id_lokalId": "p1", "postnr": "1000", "navn": "A"}])
    sz = _zip(env._cfg.work_dir / "sb.zip", [{"id_lokalId": "s1", "navn": "Sandkås"}])
    p = env.pipeline()
    run_loads(
        p,
        [Load(pn, baseline_rows([pz], pn), 100), Load(sb, baseline_rows([sz], sb), 100)],
        refresh="drop_resources",
    )
    assert cursors(p) == {"dar_postnummer": 100, "dar_supplerendebynavn": 100}

    # re-baseline dar_postnummer only; dar_supplerendebynavn's cursor + contract stay intact
    pz2 = _zip(env._cfg.work_dir / "pn2.zip", [{"id_lokalId": "p9", "postnr": "9000", "navn": "Z"}])
    run_loads(p, [Load(pn, baseline_rows([pz2], pn), 200)], refresh="drop_resources")
    assert cursors(p) == {"dar_postnummer": 200, "dar_supplerendebynavn": 100}
    assert contracts(p)["dar_postnummer"] == contract_hash(pn)
    assert contracts(p)["dar_supplerendebynavn"] == contract_hash(sb)


def _iso(year: str) -> str:
    return f"{year}-01-01T00:00:00+00:00"  # canonical utc iso, what Kind.TIMESTAMP stages


def _nvh(vejnavn: str, rfra: str, vfra: str, *, rtil: str = "") -> dict:
    # bare years, expanded to iso; extract canonicalizes them to _iso() in staging
    return {
        "id_lokalId": "v1",
        "vejnavn": vejnavn,
        "status": "3",
        "virkningFra": f"{vfra}-01-01T00:00:00Z",
        "virkningTil": "",
        "registreringFra": f"{rfra}-01-01T00:00:00Z",
        "registreringTil": f"{rtil}-01-01T00:00:00Z" if rtil else "",
    }


@_needs_db
async def test_hist_composite_key_merges_closing_delta_in_place(env: _Env):
    # a hist spec keys the dlt merge on (id, registreringfra, virkningfra): a closing delta carrying
    # the same composite key updates registreringtil in place, a new virkning inserts a new row, and
    # history is never hard-deleted
    nvh = _spec("dar_navngivenvej_hist")
    base = _zip(
        env._cfg.work_dir / "b.zip",
        [_nvh("Gammelvej", "2000", "2000"), _nvh("Tejnvej", "2015", "2015")],
    )
    p = env.pipeline()
    run_loads(p, [Load(nvh, baseline_rows([base], nvh), 100)], refresh="drop_resources")
    assert len(await env.rows("dar_navngivenvej_hist")) == 2  # both versions staged

    # close the Gammelvej registration: same (id, registreringfra, virkningfra), registreringtil set
    delta = _zip(env._cfg.work_dir / "d.zip", [_nvh("Gammelvej", "2000", "2000", rtil="2015")])
    run_loads(p, [Load(nvh, reduce_delta_files([(103, delta)], nvh), 103)])

    rows = {
        (r["id"], r["registreringfra"], r["virkningfra"]): r
        for r in await env.rows("dar_navngivenvej_hist")
    }
    assert len(rows) == 2  # no hard-delete; the close updated in place, Tejnvej untouched
    assert rows[("v1", _iso("2000"), _iso("2000"))]["registreringtil"] == _iso("2015")
    assert rows[("v1", _iso("2015"), _iso("2015"))]["registreringtil"] is None
