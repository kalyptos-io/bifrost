"""pure staging plan: per-entity BASELINE | DELTA | SKIP, and the snapshot go/no-go.

no I/O - the cli hands in catalog listings + live dlt cursors/contracts, plan_staging decides.
cursor semantics are locked: no cursor, a changed extraction contract, an empty staged table under
a committed cursor (a crash/failover lost the load behind it), a cursor stranded on a lineage we no
longer follow (generation numbers restart per data-model version), or a delta gap -> re-baseline
(cursor = newest delta - 3, overlap safe, and never past the downloaded total's own generation);
a contiguous run above the cursor -> apply it; nothing newer -> skip. needs_snapshot gates the
derive step so an unchanged cursor+contract vector (already in the watermark) never re-snapshots.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from bifrost.db.generations import Generation

from .fetch.catalog import generation, plan_deltas
from .registers import EntitySpec, contract_hash

_BASELINE_OVERLAP = 3  # re-apply the newest 3 deltas after a baseline (full-state rows idempotent)


class Action(Enum):
    BASELINE = auto()
    DELTA = auto()
    SKIP = auto()


@dataclass(frozen=True, slots=True)
class EntityPlan:
    spec: EntitySpec
    action: Action
    files: list[tuple[int, dict]]  # (generation, delta meta) ascending; empty for BASELINE/SKIP
    new_cursor: int  # BASELINE: provisional - cursor_for_total lowers it to the total's own gen


def baseline_cursor(metas: list[dict]) -> int:
    """the cursor to commit with a fresh baseline: newest available delta minus the overlap window,
    floored at 0. no deltas listed -> 0 (the next contiguous run picks up from the start)."""
    gens = [generation(m) for m in metas]
    return max(max(gens) - _BASELINE_OVERLAP, 0) if gens else 0


def cursor_for_total(table: str, metas: list[dict], total_gen: int) -> int:
    """the cursor to commit with the total actually downloaded: never past that total's own
    generation, so a lagging total is bridged by replaying every delta above it (full-state rows are
    idempotent) instead of silently skipping them. raises SystemExit only when retention already
    dropped the bridging deltas - the one gap no baseline of this total can close."""
    gens = [generation(m) for m in metas]
    if not gens:
        return 0
    oldest = min(gens)
    if oldest > total_gen + 1:
        raise SystemExit(
            f"[-] {table}: total generation {total_gen} predates the oldest available delta "
            f"{oldest}; deltas {total_gen + 1}..{oldest - 1} are past retention and staging "
            "cannot be made whole from this total"
        )
    return max(min(max(gens) - _BASELINE_OVERLAP, total_gen), 0)


def _log_gap(spec: EntitySpec, metas: list[dict], cursor: int) -> None:
    above = sorted(g for m in metas if (g := generation(m)) > cursor)
    span = f"{above[0]}..{above[-1]}" if above else "none"
    print(
        f"[!] {spec.table}: delta gap - cursor {cursor}, "
        f"available {span} (want {cursor + 1}); re-baselining"
    )


def _off_lineage(metas: list[dict], cursor: int) -> bool:
    # a cursor above everything listed was committed on a lineage we no longer follow: generation
    # numbers restart per data-model version, so it can never be reached from this one
    gens = [generation(m) for m in metas]
    return bool(gens) and cursor > max(gens)


def _log_lineage(spec: EntitySpec, metas: list[dict], cursor: int) -> None:
    gens = sorted(generation(m) for m in metas)
    print(
        f"[!] {spec.table}: cursor {cursor} is off the followed lineage "
        f"({gens[0]}..{gens[-1]}); re-baselining"
    )


def _log_contract(spec: EntitySpec, stored: str | None) -> None:
    seen = stored[:12] if stored else "unknown"
    print(f"[!] {spec.table}: extraction contract changed ({seen}); re-baselining")


def plan_staging(
    listings: Iterable[tuple[EntitySpec, list[dict]]],
    cursors: dict[str, int],
    contracts: Mapping[str, str],
    *,
    empty: frozenset[str] | set[str] = frozenset(),
) -> list[EntityPlan]:
    """decide each entity's staging action from its delta listing, committed cursor, and stored
    extraction contract. a contract mismatch re-baselines that resource only (drops its table +
    state via refresh="drop_resources"); the load rewrites the current contract. `empty` names
    staged tables observed empty: a committed cursor over one means the load behind it was lost."""
    plans: list[EntityPlan] = []
    for spec, metas in listings:
        cursor = cursors.get(spec.table)
        if cursor is None:  # never staged -> full total
            plans.append(EntityPlan(spec, Action.BASELINE, [], baseline_cursor(metas)))
            continue
        if spec.table in empty:
            print(f"[!] {spec.table}: cursor {cursor} committed but table is empty; re-baselining")
            plans.append(EntityPlan(spec, Action.BASELINE, [], baseline_cursor(metas)))
            continue
        stored = contracts.get(spec.table)
        if stored != contract_hash(spec):  # semantics changed (or unknown) -> full total
            _log_contract(spec, stored)
            plans.append(EntityPlan(spec, Action.BASELINE, [], baseline_cursor(metas)))
            continue
        if _off_lineage(metas, cursor):
            _log_lineage(spec, metas, cursor)
            plans.append(EntityPlan(spec, Action.BASELINE, [], baseline_cursor(metas)))
            continue
        plan = plan_deltas(metas, cursor)
        if plan.gap:  # retention dropped a needed delta -> full total, re-baseline cursor
            _log_gap(spec, metas, cursor)
            plans.append(EntityPlan(spec, Action.BASELINE, [], baseline_cursor(metas)))
        elif plan.files:
            files = [(generation(m), m) for m in plan.files]
            plans.append(EntityPlan(spec, Action.DELTA, files, files[-1][0]))
        else:  # up to date
            plans.append(EntityPlan(spec, Action.SKIP, [], cursor))
    return plans


def _live_state(
    cursors: dict[str, int], contracts: Mapping[str, str]
) -> dict[str, tuple[int, str | None]]:
    return {t: (g, contracts.get(t)) for t, g in cursors.items()}


def _watermark_state(watermark: Mapping[str, Any]) -> dict[str, tuple[int, str | None]]:
    return {t: (v["gen"], v.get("contract")) for t, v in watermark.items()}


def needs_snapshot(
    cursors: dict[str, int],
    contracts: Mapping[str, str],
    watermark: Mapping[str, Any],
    current_generation: Generation | None,
    fingerprint: str,
) -> bool:
    """derive a fresh generation iff the staging cursor+contract vector moved since the last
    watermark, or no live generation matches this build's shape (a shape bump, or a never-seeded
    db). a contract-only change (same cursors, new extraction semantics) still triggers a build."""
    if _live_state(cursors, contracts) != _watermark_state(watermark):
        return True
    return current_generation is None or current_generation.shape != fingerprint
