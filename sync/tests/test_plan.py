"""pure staging/snapshot decisions: every plan_staging branch (incl. a cursor stranded on a
retired data-model lineage) and needs_snapshot's cursor/shape gates. no db/net."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from bifrost.db.generations import Generation
from bifrost_sync.plan import (
    Action,
    baseline_cursor,
    cursor_for_total,
    needs_snapshot,
    plan_staging,
)
from bifrost_sync.registers import (
    ALL_ENTITIES,
    Column,
    Currency,
    EntitySpec,
    contract_hash,
)

# catalog.lineage_deltas hands the planner ONE lineage; dagi's live lineage carries the LOWER
# generations (its retired lineage sits up at 729..743 on the old shared counter)
_DAGI_LIVE = list(range(175, 190))  # [175..189]


def _spec(table: str):
    return next(s for s in ALL_ENTITIES if s.table == table)


def _metas(gens: list[int]) -> list[dict]:
    return [{"generationNumber": g} for g in gens]


def _plan(table: str, gens: list[int], cursor: int | None, contracts: dict | None = None):
    spec = _spec(table)
    cursors = {} if cursor is None else {table: cursor}
    # default: stored contract == current, so the contract branch never fires (cursor-logic tests)
    if contracts is None:
        contracts = {table: contract_hash(spec)}
    [p] = plan_staging([(spec, _metas(gens))], cursors, contracts)
    return p


# plan_staging branches


def test_no_cursor_baselines_at_newest_minus_overlap():
    p = _plan("dar_adresse", [688, 689, 690], cursor=None)
    assert p.action is Action.BASELINE
    assert p.files == []
    assert p.new_cursor == 687  # 690 - 3


def test_contiguous_run_above_cursor_is_delta():
    p = _plan("dar_adresse", [686, 687, 688, 689], cursor=687)
    assert p.action is Action.DELTA
    assert [g for g, _ in p.files] == [688, 689]
    assert p.new_cursor == 689  # newest applied


def test_nothing_newer_skips_and_holds_cursor():
    p = _plan("dar_adresse", [688, 689, 690], cursor=690)
    assert p.action is Action.SKIP
    assert p.files == []
    assert p.new_cursor == 690


def test_committed_cursor_over_empty_table_rebaselines():
    spec = _spec("dar_adresse")
    [p] = plan_staging(
        [(spec, _metas([688, 689, 690]))],
        {"dar_adresse": 690},
        {"dar_adresse": contract_hash(spec)},
        empty={"dar_adresse"},
    )
    assert p.action is Action.BASELINE
    assert p.new_cursor == 687  # 690 - 3


def test_empty_without_cursor_stays_plain_baseline():
    [p] = plan_staging([(_spec("dar_adresse"), _metas([690]))], {}, {}, empty={"dar_adresse"})
    assert p.action is Action.BASELINE


def test_missing_first_delta_is_a_gap_rebaseline():
    # retention dropped 688/689: run starts at 690 != cursor+1 -> gap
    p = _plan("dar_adresse", [690, 691], cursor=687)
    assert p.action is Action.BASELINE
    assert p.new_cursor == 688  # 691 - 3


def test_hole_inside_run_is_a_gap_rebaseline():
    p = _plan("dar_adresse", [688, 690], cursor=687)  # 689 missing
    assert p.action is Action.BASELINE
    assert p.files == []


# dagi lineage handling


def test_dagi_cursor_in_the_live_lineage_is_delta():
    p = _plan("dagi_kommuneinddeling", _DAGI_LIVE, cursor=185)
    assert p.action is Action.DELTA
    assert [g for g, _ in p.files] == [186, 187, 188, 189]
    assert p.new_cursor == 189


def test_cursor_stranded_on_a_retired_lineage_rebaselines():
    # 743 was committed on the retired lineage's own counter; the live one tops out at 189, so
    # nothing above it will ever arrive. must re-baseline, not skip forever.
    p = _plan("dagi_kommuneinddeling", _DAGI_LIVE, cursor=743)
    assert p.action is Action.BASELINE
    assert p.new_cursor == 186  # 189 - 3


def test_dagi_cursor_at_the_lineage_head_skips():
    p = _plan("dagi_kommuneinddeling", _DAGI_LIVE, cursor=189)
    assert p.action is Action.SKIP


def test_cursor_below_the_lineage_gaps():
    # retention already dropped everything back to 175; 101 can no longer be bridged
    p = _plan("dagi_kommuneinddeling", _DAGI_LIVE, cursor=100)
    assert p.action is Action.BASELINE
    assert p.new_cursor == 186


def test_plan_staging_is_per_entity():
    listings = [
        (_spec("dar_adresse"), _metas([688, 689])),
        (_spec("dar_postnummer"), _metas([688, 689, 690])),
    ]
    contracts = {t: contract_hash(_spec(t)) for t in ("dar_adresse", "dar_postnummer")}
    plans = plan_staging(listings, {"dar_adresse": 688, "dar_postnummer": 690}, contracts)
    assert [p.action for p in plans] == [Action.DELTA, Action.SKIP]


# contract-driven baseline


def test_contract_change_forces_selective_baseline():
    # cursor is current (would SKIP) but the stored contract differs -> re-baseline this resource
    p = _plan("dar_adresse", [688, 689, 690], cursor=690, contracts={"dar_adresse": "stale"})
    assert p.action is Action.BASELINE
    assert p.new_cursor == 687  # 690 - 3


def test_contract_change_isolates_to_the_changed_resource():
    listings = [
        (_spec("dar_adresse"), _metas([688, 689, 690])),
        (_spec("dar_postnummer"), _metas([688, 689, 690])),
    ]
    # only dar_adresse's contract drifted; dar_postnummer keeps its normal SKIP
    contracts = {"dar_adresse": "stale", "dar_postnummer": contract_hash(_spec("dar_postnummer"))}
    plans = plan_staging(listings, {"dar_adresse": 690, "dar_postnummer": 690}, contracts)
    assert [p.action for p in plans] == [Action.BASELINE, Action.SKIP]


def test_unknown_table_missing_contract_baselines():
    # a table absent from both dlt state and the manifest -> unknown -> full re-baseline
    spec = EntitySpec("DAR", "Fake", "dar_fake", (Column("id", "id_lokalId"),), Currency.DAR)
    [p] = plan_staging([(spec, _metas([5, 6, 7]))], {"dar_fake": 7}, {})
    assert p.action is Action.BASELINE
    assert p.new_cursor == 4  # 7 - 3


# baseline_cursor edges


def test_baseline_cursor_floors_at_zero():
    assert baseline_cursor([]) == 0
    assert baseline_cursor(_metas([1, 2])) == 0  # 2 - 3 clamps to 0


# cursor_for_total: the committed cursor can never outrun the total it was built from


def test_fresh_total_keeps_the_overlap_cursor():
    metas = _metas(list(range(680, 693)))  # 680..692
    assert cursor_for_total("dar_adresse", metas, total_gen=692) == 689  # 692 - 3, unchanged
    assert cursor_for_total("dar_adresse", metas, total_gen=690) == 689  # still ahead of the cursor


def test_lagging_total_drops_the_cursor_to_the_total():
    # the total is older than the overlap window: commit its own generation so the next run applies
    # 686..692 instead of skipping them
    metas = _metas(list(range(680, 693)))
    assert cursor_for_total("dar_adresse", metas, total_gen=685) == 685
    assert cursor_for_total("dar_adresse", metas, total_gen=679) == 679  # exactly at oldest - 1


def test_unbridgeable_gap_between_total_and_retention_fails_loud():
    # deltas 671..678 are past retention, so no replay can carry the 678-total up to 692
    metas = _metas(list(range(679, 693)))
    with pytest.raises(SystemExit, match="past retention"):
        cursor_for_total("dar_adresse", metas, total_gen=677)


def test_cursor_for_total_floors_at_zero_and_tolerates_no_deltas():
    assert cursor_for_total("dar_adresse", [], total_gen=99) == 0
    assert cursor_for_total("dar_adresse", _metas([1, 2]), total_gen=2) == 0  # 2 - 3 clamps


# needs_snapshot


def _gen(shape: str) -> Generation:
    return Generation("gen_x", shape, 1, 1, 1, 1, 1, datetime.now(UTC))


def _wm(gen: int, contract: str | None) -> dict:
    return {"gen": gen, "contract": contract}


def test_needs_snapshot_when_cursor_vector_moved():
    assert needs_snapshot({"a": 2}, {}, {"a": _wm(1, None)}, _gen("fp"), "fp") is True


def test_needs_snapshot_when_no_matching_generation():
    assert needs_snapshot({"a": 1}, {}, {"a": _wm(1, None)}, None, "fp") is True


def test_needs_snapshot_when_generation_shape_is_stale():
    assert needs_snapshot({"a": 1}, {}, {"a": _wm(1, None)}, _gen("old"), "fp") is True


def test_no_snapshot_when_watermark_current_and_shape_matches():
    assert needs_snapshot({"a": 1}, {}, {"a": _wm(1, None)}, _gen("fp"), "fp") is False


def test_needs_snapshot_on_contract_change_with_same_cursor():
    # cursor unchanged, but the live contract differs from the watermark's -> rebuild
    live = {"dar_adresse": "newhash"}
    wm = {"dar_adresse": _wm(5, "oldhash")}
    assert needs_snapshot({"dar_adresse": 5}, live, wm, _gen("fp"), "fp") is True


# contract hash


def test_contract_hash_is_deterministic_and_field_sensitive():
    from dataclasses import replace

    spec = _spec("dar_adresse")
    assert contract_hash(spec) == contract_hash(spec)  # stable across calls
    assert contract_hash(replace(spec, currency=Currency.OPEN)) != contract_hash(spec)
    assert contract_hash(replace(spec, delta_retention_days=99)) != contract_hash(spec)
    assert contract_hash(replace(spec, columns=spec.columns[:-1])) != contract_hash(spec)
