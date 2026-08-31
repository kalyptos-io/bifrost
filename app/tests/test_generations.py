"""dataset generation helpers and the shared compatibility fingerprint contract."""

from datetime import UTC, datetime, timedelta

from bifrost.db.contracts import CURRENT, PREVIOUS, Contract
from bifrost.db.generations import (
    Generation,
    _schema_ts,
    gc_targets,
    schema_name_for,
    select_current,
    should_swap,
)
from bifrost.db.shape import build_fingerprint

NOW = datetime(2026, 7, 4, 13, 0, 0, tzinfo=UTC)

# synthetic contract pair for selection/gc tests (production defaults stay CURRENT/PREVIOUS)
CUR = Contract(2, "shape-cur")
PREV = Contract(1, "shape-prev")


def _gen(name: str, shape: str, age: timedelta, *, version: int = CUR.version) -> Generation:
    return Generation(
        name, shape, 4_000_000, 3_200, 1_200_000, 140_000, 2_700_000, NOW - age, version
    )


# compatibility contract


def test_current_contract_matches_build_fingerprint() -> None:
    assert build_fingerprint() == CURRENT.fingerprint, (
        "build_fingerprint() drifted from CURRENT.fingerprint: add a new Contract as CURRENT and "
        "demote the old CURRENT to PREVIOUS in bifrost.db.contracts"
    )


def test_previous_is_the_demoted_contract() -> None:
    assert PREVIOUS is not None and PREVIOUS.version == CURRENT.version - 1


# ---- schema-name codec ----


def test_schema_name_round_trips() -> None:
    ts = datetime(2026, 7, 4, 12, 0, 0, 123456, tzinfo=UTC)
    name = schema_name_for(ts)
    assert name == "gen_20260704120000123456"
    assert _schema_ts(name) == ts


def test_schema_ts_ignores_non_gen_and_garbage() -> None:
    assert _schema_ts("public") is None
    assert _schema_ts("gen_notatimestamp") is None


# ---- gc_targets (contract-aware retention: 2 current, 1 previous, newest 1 per retired pair) ----


def test_gc_keeps_newest_two_current_and_one_previous() -> None:
    regs = [
        _gen("gen_c1", CUR.fingerprint, timedelta(minutes=5)),  # newest current -> keep
        _gen("gen_c2", CUR.fingerprint, timedelta(hours=11)),  # 2nd current -> keep
        _gen("gen_c3", CUR.fingerprint, timedelta(hours=12)),  # 3rd current, past grace -> drop
        _gen("gen_p1", PREV.fingerprint, timedelta(minutes=5), version=PREV.version),  # keep (1)
        _gen(
            "gen_p2", PREV.fingerprint, timedelta(hours=12), version=PREV.version
        ),  # 2nd prev drop
    ]
    names = [g.schema_name for g in regs]
    assert set(gc_targets(names, regs, NOW, current=CUR, previous=PREV)) == {"gen_c3", "gen_p2"}


def test_gc_keeps_newest_of_a_retired_pair() -> None:
    # a retired pair (neither current nor previous) keeps its newest as a rollback floor; only the
    # older past-grace member is dropped
    ret = Contract(0, "shape-ancient")
    regs = [
        _gen("gen_cur", CUR.fingerprint, timedelta(minutes=5)),  # current -> keep
        _gen("gen_r_new", ret.fingerprint, timedelta(hours=11), version=ret.version),  # newest keep
        _gen("gen_r_old", ret.fingerprint, timedelta(hours=12), version=ret.version),  # older drop
    ]
    names = [g.schema_name for g in regs]
    assert gc_targets(names, regs, NOW, current=CUR, previous=PREV) == ["gen_r_old"]


def test_gc_retired_floor_survives_previous_none() -> None:
    # with previous=None a contract bump must not GC the prior pair's last generation (rollback)
    regs = [
        _gen("gen_cur", CUR.fingerprint, timedelta(minutes=5)),  # current -> keep
        _gen("gen_old", PREV.fingerprint, timedelta(hours=12), version=PREV.version),  # floor
    ]
    names = [g.schema_name for g in regs]
    assert gc_targets(names, regs, NOW, current=CUR, previous=None) == []


def test_gc_retired_floor_is_per_pair() -> None:
    # each retired pair keeps its own newest; older past-grace members of both pairs are dropped
    pair_a = Contract(0, "shape-ancient")
    pair_b = Contract(3, "shape-legacy")
    regs = [
        _gen("gen_a_new", pair_a.fingerprint, timedelta(hours=10), version=pair_a.version),  # keep
        _gen("gen_a_old", pair_a.fingerprint, timedelta(hours=12), version=pair_a.version),  # drop
        _gen("gen_b_new", pair_b.fingerprint, timedelta(hours=11), version=pair_b.version),  # keep
        _gen("gen_b_old", pair_b.fingerprint, timedelta(hours=13), version=pair_b.version),  # drop
    ]
    names = [g.schema_name for g in regs]
    got = set(gc_targets(names, regs, NOW, current=CUR, previous=PREV))
    assert got == {"gen_a_old", "gen_b_old"}


def test_gc_spares_superseded_within_grace() -> None:
    regs = [
        _gen("gen_a", CUR.fingerprint, timedelta(minutes=1)),
        _gen("gen_b", CUR.fingerprint, timedelta(minutes=2)),
        _gen(
            "gen_c", CUR.fingerprint, timedelta(minutes=3)
        ),  # 3rd but still within grace -> spared
    ]
    names = [g.schema_name for g in regs]
    assert gc_targets(names, regs, NOW, current=CUR, previous=PREV) == []


def test_gc_spares_a_held_schema() -> None:
    # a superseded, past-grace generation with a live serving lease is never dropped
    regs = [
        _gen("gen_a", CUR.fingerprint, timedelta(minutes=5)),
        _gen("gen_b", CUR.fingerprint, timedelta(hours=11)),
        _gen("gen_c", CUR.fingerprint, timedelta(hours=12)),  # 3rd, past grace, but a pod serves it
    ]
    names = [g.schema_name for g in regs]
    got = gc_targets(names, regs, NOW, current=CUR, previous=PREV, held=frozenset({"gen_c"}))
    assert got == []


def test_gc_drops_aged_orphans_only() -> None:
    # a gen_* schema with no registry row is a dead partial load; drop only once past grace
    young = schema_name_for(NOW - timedelta(minutes=20))
    old = schema_name_for(NOW - timedelta(hours=5))
    schemas = [young, old, "gen_unparseable"]
    assert gc_targets(schemas, [], NOW) == [old]  # young spared, garbage untouched
    assert gc_targets([old], [], NOW, held=frozenset({old})) == []  # a lease pins even an orphan


# ---- should_swap (refresh-tick cutover decision) ----


def test_should_swap_to_a_different_schema() -> None:
    cand = _gen("gen_new", CUR.fingerprint, timedelta(minutes=1))
    assert should_swap("gen_old", CUR.version, cand) is True


def test_should_not_swap_to_the_same_schema_or_none() -> None:
    cand = _gen("gen_same", CUR.fingerprint, timedelta(minutes=1))
    assert should_swap("gen_same", CUR.version, cand) is False
    assert should_swap("gen_same", CUR.version, None) is False


def test_should_not_downgrade_to_an_older_contract() -> None:
    # serving current; a previous-contract candidate must never win a refresh
    prev = _gen("gen_prev", PREV.fingerprint, timedelta(minutes=1), version=PREV.version)
    assert should_swap("gen_cur", CUR.version, prev) is False


def test_should_upgrade_previous_to_current() -> None:
    # serving previous; a current-contract candidate is an upgrade and swaps
    cur = _gen("gen_cur", CUR.fingerprint, timedelta(minutes=1))
    assert should_swap("gen_prev", PREV.version, cur) is True


# ---- select_current (fake conn) ----


class _FakeConn:
    def __init__(self, generations=None):
        self.generations = generations  # {(contract_version, shape): row-dict} or None (absent)

    async def fetchval(self, sql, *args):
        if "to_regclass('public.generations')" in sql:
            return "public.generations" if self.generations is not None else None
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetchrow(self, sql, *args):
        if "FROM public.generations" in sql:
            return (self.generations or {}).get((args[0], args[1]))  # WHERE contract_version, shape
        raise AssertionError(f"unexpected fetchrow: {sql}")


def _gen_row(name, contract):
    return {
        "schema_name": name,
        "shape": contract.fingerprint,
        "row_count": 4_000_000,
        "area_count": 3_200,
        "matrikel_count": 1_200_000,
        "stednavne_count": 140_000,
        "ejendom_count": 2_700_000,
        "contract_version": contract.version,
        "seeded_at": NOW,
    }


def _key(contract) -> tuple[int, str]:
    return (contract.version, contract.fingerprint)


async def test_select_current_returns_matching_current_generation() -> None:
    conn = _FakeConn(generations={_key(CUR): _gen_row("gen_new", CUR)})
    gen = await select_current(conn, current=CUR, previous=PREV)
    assert gen is not None and gen.schema_name == "gen_new"
    assert gen.contract_version == CUR.version and gen.shape == CUR.fingerprint


async def test_select_current_prefers_current_over_previous() -> None:
    conn = _FakeConn(
        generations={_key(CUR): _gen_row("gen_cur", CUR), _key(PREV): _gen_row("gen_prev", PREV)}
    )
    gen = await select_current(conn, current=CUR, previous=PREV)
    assert gen is not None and gen.schema_name == "gen_cur"  # current wins regardless of previous


async def test_select_current_falls_back_to_previous() -> None:
    conn = _FakeConn(generations={_key(PREV): _gen_row("gen_prev", PREV)})
    gen = await select_current(conn, current=CUR, previous=PREV)
    assert gen is not None and gen.schema_name == "gen_prev"
    assert gen.contract_version == PREV.version


async def test_select_current_none_on_shape_drift_no_legacy() -> None:
    conn = _FakeConn(
        generations={(9, "other-shape"): _gen_row("gen_old", Contract(9, "other-shape"))}
    )
    assert await select_current(conn, current=CUR, previous=PREV) is None
