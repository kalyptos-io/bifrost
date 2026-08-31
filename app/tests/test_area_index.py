"""AreaIndex: in-memory kind-scoped ranking + exact-code path, with pg_trgm parity (DB-gated).

the tokenizer itself is parity-tested in test_street_index (shared module); here we pin the
kind-scoped knn order + the by_code path. the DB-gated test confirms the area ranking matches pg.
"""

import os
from uuid import uuid4

import asyncpg
import pytest
from bifrost.arms.area_index import AreaHit, AreaIndex
from bifrost.db import ADMIN_AREA_COLUMNS, schema_sql

_DSN = os.environ.get("BIFROST_DATABASE_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="BIFROST_DATABASE_DSN unset")

# (area_id, kind, code, name, folded_name)
_ROWS = [
    ("a-kbh", "kommune", "0101", "København", "kobenhavn"),
    ("a-kbhs", "kommune", "0240", "Københavns Omegn", "kobenhavns omegn"),
    ("a-frb", "kommune", "0147", "Frederiksberg", "frederiksberg"),
    ("p-2100", "postcode", "2100", "København Ø", "kobenhavn o"),
    ("r-hs", "region", "1084", "Hovedstaden", "hovedstaden"),
]


def _idx() -> AreaIndex:
    return AreaIndex(_ROWS)


def test_knn_ranks_within_kind_and_excludes_other_kinds():
    hits = _idx().knn("kobenhavn", kind="kommune")
    assert hits[0] == AreaHit("a-kbh", "kommune", "0101", "København", 1.0)  # exact name
    assert hits[1].area_id == "a-kbhs" and 0.1 < hits[1].sim < 1.0  # partial
    assert {h.kind for h in hits} == {"kommune"}  # postcode "kobenhavn o" not pulled in


def test_knn_kind_filter_scopes_the_gazetteer():
    hits = _idx().knn("kobenhavn", kind="postcode")
    assert [h.area_id for h in hits] == ["p-2100"]  # only the postcode row, despite name overlap


def test_knn_cap_limits_hits():
    assert len(_idx().knn("kobenhavn", kind="kommune", cap=1)) == 1


def test_knn_threshold_prunes_below_floor():
    assert _idx().knn("zzzzzz", kind="kommune") == []


def test_knn_empty_query_returns_nothing():
    assert _idx().knn("", kind="kommune") == []


def test_knn_equal_sim_tiebreaks_on_area_id():
    idx = AreaIndex([("z1", "sogn", None, "X", "alpha"), ("a1", "sogn", None, "Y", "alpha")])
    assert [h.area_id for h in idx.knn("alpha", kind="sogn")] == ["a1", "z1"]  # asc area_id


def test_by_code_exact_lookup():
    assert _idx().by_code("2100", kind="postcode") == [
        AreaHit("p-2100", "postcode", "2100", "København Ø", 1.0)
    ]
    assert _idx().by_code("0101", kind="kommune")[0].area_id == "a-kbh"
    assert _idx().by_code("9999", kind="postcode") == []  # unknown code -> empty


# ---- lifecycle: alias union + filter ----


def test_knn_alias_union_and_lifecycle_filter():
    # disjoint names so the alias query can't fuzzily hit the canonical (isolates the alias path)
    idx = AreaIndex(
        [("k1", "kommune", "0101", "Alpha", "alpha")],
        lifecycles={"k1": "current"},
        alias_rows=[("k1", "kommune", "Beta", "beta", "retired")],
    )
    assert idx.knn("beta", kind="kommune", lifecycle=("current",)) == []  # alias filtered out
    ret = idx.knn("beta", kind="kommune", lifecycle=("retired",))
    # the historical name resolves the canonical area_id (no code) with the alias lifecycle
    assert ret == [AreaHit("k1", "kommune", None, "Beta", 1.0, "retired")]


def test_knn_drops_non_current_canonical_by_default():
    idx = AreaIndex([("k1", "kommune", "0101", "Nedlagt", "nedlagt")], lifecycles={"k1": "retired"})
    assert idx.knn("nedlagt", kind="kommune") == []  # a retired area is absent from a current query
    assert idx.knn("nedlagt", kind="kommune", lifecycle=("retired",))[0].area_id == "k1"


# ---- pg_trgm parity (DB-gated) ----

_PG_RANK = (
    "SELECT area_id, similarity(folded_name, $1) AS sim FROM admin_area "
    "WHERE kind = $2 AND folded_name % $1 ORDER BY folded_name <-> $1, area_id LIMIT $3"
)


# admin_area.geometry is NOT NULL; the ranking ignores it, so a placeholder geojson satisfies COPY
_DB_ROWS = [(*r, '{"type":"Polygon","coordinates":[]}') for r in _ROWS]


@pytest.fixture
async def db():
    # throwaway gen_test_* schema: unqualified admin_area + pg_trgm resolve via the search_path
    schema = f"gen_test_{uuid4().hex}"
    pool = await asyncpg.create_pool(_DSN, server_settings={"search_path": f"{schema}, public"})
    async with pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(schema_sql())
        await conn.copy_records_to_table(
            "admin_area", records=_DB_ROWS, columns=ADMIN_AREA_COLUMNS[:-1]
        )
    yield AreaIndex(_ROWS), pool
    async with pool.acquire() as conn:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await pool.close()


@_needs_db
async def test_knn_matches_pg_rank(db):
    index, pool = db
    async with pool.acquire() as conn:
        await conn.execute("SET pg_trgm.similarity_threshold = 0.1")
        cases = (("kobenhavn", "kommune"), ("hovedstaden", "region"), ("kobenhavn", "postcode"))
        for q, kind in cases:
            pg = await conn.fetch(_PG_RANK, q, kind, 64)
            mine = index.knn(q, kind=kind, cap=64)
            assert [h.area_id for h in mine] == [r["area_id"] for r in pg], (q, kind)
            for h, r in zip(mine, pg, strict=True):
                assert abs(h.sim - r["sim"]) < 1e-6, (q, h)
