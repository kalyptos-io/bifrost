"""StreetIndex: in-memory ranking + pg_trgm parity.

the parity suite pins the numpy index against live pg_trgm over a throwaway gen_test_* schema (never
public), so it skips unless BIFROST_DATABASE_DSN is set but never touches a real seed.
"""

import os
from uuid import uuid4

import asyncpg
import pytest
from bifrost.arms.street_index import Combo, StreetIndex, _trigrams
from bifrost.db import STREET_DIM_COLUMNS, schema_sql

_DSN = os.environ.get("BIFROST_DATABASE_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="BIFROST_DATABASE_DSN unset")


# ---- pure: tokenizer ----


def test_trigrams_single_word_matches_pg_show_trgm():
    assert _trigrams("cat") == {"  c", " ca", "cat", "at "}


def test_trigrams_pads_each_word_no_cross_word():
    # pg_trgm pads words independently; no trigram spans the space
    assert _trigrams("foo bar") == {"  f", " fo", "foo", "oo ", "  b", " ba", "bar", "ar "}


def test_trigrams_empty_and_punctuation():
    assert _trigrams("") == set()
    assert _trigrams("  ") == set()  # no alnum runs


# ---- pure: index ranking ----


def _idx() -> StreetIndex:
    dim = [(0, "Alpha", "alpha"), (1, "Alphabet", "alphabet"), (2, "Beta", "beta")]
    bridge = [(0, "1000"), (0, "2000"), (1, "1000"), (2, "3000")]
    return StreetIndex(dim, bridge)


def test_knn_descends_in_similarity_and_expands_postcodes():
    combos = _idx().knn("alpha", cap=64)
    # exact street first, both its postcodes (asc), then the partial match
    assert combos[0] == Combo(0, "1000", 1.0, "Alpha", "alpha")
    assert combos[1] == Combo(0, "2000", 1.0, "Alpha", "alpha")
    assert combos[2].street_id == 1  # alphabet, lower similarity
    sims = [c.sim for c in combos]
    assert sims == sorted(sims, reverse=True)


def test_knn_cap_counts_combos_not_streets():
    combos = _idx().knn("alpha", cap=2)
    assert len(combos) == 2  # both slots consumed by street 0's two postcodes
    assert {c.street_id for c in combos} == {0}


def test_knn_postcode_filter_drops_unconfined_combos():
    combos = _idx().knn("alpha", cap=64, postcodes={"2000"})
    assert combos == [Combo(0, "2000", 1.0, "Alpha", "alpha")]  # street 1 (only 1000) excluded


def test_knn_unknown_postcode_pin_returns_nothing():
    assert _idx().knn("alpha", cap=64, postcodes={"9999"}) == []


def test_knn_postcode_pin_keeps_order_across_mixed_entries():
    # 1000 is shared by Alpha and Alphabet; sim order and the expansion survive the pin
    combos = _idx().knn("alpha", cap=64, postcodes={"1000"})
    assert [(c.street_id, c.postcode) for c in combos] == [(0, "1000"), (1, "1000")]


def test_knn_threshold_prunes_below_floor():
    assert _idx().knn("zzzzzz", cap=64) == []  # no trigram overlap -> sim 0 < THRESHOLD


def test_knn_empty_query_returns_nothing():
    assert _idx().knn("", cap=64) == []


def test_knn_equal_sim_tiebreaks_on_street_id():
    idx = StreetIndex([(2, "X", "alpha"), (0, "Y", "alpha")], [(2, "1000"), (0, "1000")])
    assert [c.street_id for c in idx.knn("alpha", cap=64)] == [0, 2]  # asc street_id


def test_dims_resolves_display_and_skips_unknown():
    dims = _idx().dims([0, 1, 99], "alpha")
    assert 99 not in dims
    assert dims[0].street == "Alpha" and dims[0].sim == 1.0
    assert dims[1].street == "Alphabet" and 0.0 < dims[1].sim < 1.0


def test_dims_empty_query_scores_zero():
    assert _idx().dims([0], "").get(0).sim == 0.0


# ---- lifecycle: alias union + filter-before-cap ----


def test_knn_alias_union_carries_lifecycle_and_own_postcodes():
    # disjoint names so the alias query can't fuzzily hit the canonical (isolates the alias path)
    idx = StreetIndex(
        [(0, "Alpha", "alpha")],
        [(0, "1000")],
        alias_rows=[("Beta", "beta", 0, ["2000"], "retired")],
    )
    assert idx.knn("beta", cap=64, lifecycle=("current",)) == []  # retired alias filtered out
    ret = idx.knn("beta", cap=64, lifecycle=("retired",))
    # the alias surfaces under the canonical street_id, its own postcode, and the alias lifecycle
    assert ret == [Combo(0, "2000", 1.0, "Beta", "beta", "retired")]


def test_knn_canonical_combo_carries_no_alias_lifecycle():
    idx = StreetIndex([(0, "Nygade", "nygade")], [(0, "1000")])
    assert idx.knn("nygade", cap=64) == [Combo(0, "1000", 1.0, "Nygade", "nygade", None)]


def test_knn_lifecycle_filters_before_cap():
    # the retired alias is the exact match (would rank first + take cap=1); a current-only request
    # must drop it BEFORE the cap so the canonical still fills the slot, not an empty result
    idx = StreetIndex(
        [(0, "Abcd", "abcd")],
        [(0, "1000")],
        alias_rows=[("Abc", "abc", 0, ["2000"], "retired")],
    )
    combos = idx.knn("abc", cap=1, lifecycle=("current",))
    assert [c.folded_street for c in combos] == ["abcd"]


# ---- pg_trgm parity (DB-gated) ----

_PARITY_STREETS = [
    (0, "Lindealle", "lindealle"),
    (1, "Lindealley", "lindealley"),
    (2, "Søren Frichs Vej", "soeren frichs vej"),
    (3, "Strandlodsvej", "strandlodsvej"),
    (4, "Hammer Bakker", "hammer bakker"),
]
_PARITY_BRIDGE = [(0, "6900"), (0, "8000"), (1, "6900"), (2, "8000"), (3, "2300"), (4, "9000")]
_PARITY_QUERIES = [
    "lindealle",
    "lindealley",
    "lindealen",  # typo, both lindeall* in range
    "soeren frichs vej",
    "hammer bakker",
    "hammerbakker",  # one-word typo of the two-word street
    "strandlodsvej",
    "zzzqqq",  # no match
]


@pytest.fixture
async def db():
    # throwaway gen_test_* schema: unqualified table refs + pg_trgm resolve via the search_path
    schema = f"gen_test_{uuid4().hex}"
    pool = await asyncpg.create_pool(_DSN, server_settings={"search_path": f"{schema}, public"})
    async with pool.acquire() as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(schema_sql())
        await conn.copy_records_to_table(
            "street_dim", records=_PARITY_STREETS, columns=STREET_DIM_COLUMNS[:-1]
        )
        await conn.executemany(
            "INSERT INTO street_postcode (street_id, postcode) VALUES ($1, $2)", _PARITY_BRIDGE
        )
    index = StreetIndex(_PARITY_STREETS, _PARITY_BRIDGE)
    yield index, pool
    async with pool.acquire() as conn:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
    await pool.close()


_PG_COMBOS = (
    "SELECT d.street_id, sp.postcode, similarity(d.folded_street, $1) AS sim "
    "FROM street_dim d JOIN street_postcode sp USING (street_id) "
    "WHERE d.folded_street % $1 ORDER BY d.folded_street <-> $1, d.street_id, sp.postcode LIMIT $2"
)


@_needs_db
async def test_trigrams_match_show_trgm_over_seeded_streets(db):
    _, pool = db
    for _, _, folded in _PARITY_STREETS:
        pg = set(await pool.fetchval("SELECT show_trgm($1)", folded))
        assert _trigrams(folded) == pg, folded


@_needs_db
@pytest.mark.parametrize("cap", [64, 2])
async def test_knn_matches_pg_combos(db, cap):
    index, pool = db
    async with pool.acquire() as conn:
        await conn.execute("SET pg_trgm.similarity_threshold = 0.1")
        for q in _PARITY_QUERIES:
            pg = await conn.fetch(_PG_COMBOS, q, cap)
            mine = index.knn(q, cap=cap)
            assert [(c.street_id, c.postcode) for c in mine] == [
                (r["street_id"], r["postcode"]) for r in pg
            ], q
            for c, r in zip(mine, pg, strict=True):
                assert abs(c.sim - r["sim"]) < 1e-6, (q, c)


@_needs_db
async def test_knn_postcode_confined_matches_pg(db):
    index, pool = db
    confined = (
        "SELECT d.street_id, sp.postcode FROM street_dim d JOIN street_postcode sp USING "
        "(street_id) WHERE d.folded_street % $1 AND sp.postcode = ANY($3) "
        "ORDER BY d.folded_street <-> $1, d.street_id, sp.postcode LIMIT $2"
    )
    async with pool.acquire() as conn:
        await conn.execute("SET pg_trgm.similarity_threshold = 0.1")
        pg = await conn.fetch(confined, "lindealle", 64, ["6900"])
        mine = index.knn("lindealle", cap=64, postcodes={"6900"})
        assert [(c.street_id, c.postcode) for c in mine] == [
            (r["street_id"], r["postcode"]) for r in pg
        ]


@_needs_db
async def test_dims_match_pg_similarity(db):
    index, pool = db
    sids = [0, 1, 2]
    pg = {
        r["street_id"]: r["sim"]
        for r in await pool.fetch(
            "SELECT street_id, similarity(folded_street, $1) AS sim FROM street_dim "
            "WHERE street_id = ANY($2)",
            "lindealle",
            sids,
        )
    }
    mine = index.dims(sids, "lindealle")
    for sid in sids:
        assert abs(mine[sid].sim - pg[sid]) < 1e-6, sid
