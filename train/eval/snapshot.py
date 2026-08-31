"""build bounded candidate snapshots for synthetic score calibration."""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from pathlib import Path

from bifrost.arms import segmenter
from bifrost.arms.decompose import decompose
from bifrost.arms.normalize import normalize
from bifrost.arms.repository import NotSeeded, PostgresAddressSource, SourceSnapshot
from bifrost.composition import build_resolution
from bifrost.config import Settings
from bifrost.core.merge import STREET_BATCH, STREET_STREAM_CAP, _belief_value, _recovery_fetches
from bifrost.core.ports import BeliefBranch
from bifrost.core.types import CURRENT_LIFECYCLE, AddressRow, Axis, Belief, Capability, Grade

from .calibrate import _score
from .provenance import manifest
from .run import _HARD, read_jsonl

_PARAMS = Path(__file__).resolve().parents[2] / "app/src/bifrost/db/artifacts/score_params.json"


def _beliefs(query: str, branches: tuple[BeliefBranch, ...]) -> tuple[Belief, ...]:
    decomposition = decompose(normalize(query))
    return tuple(belief for branch in branches if (belief := branch(decomposition)) is not None)


async def _pool(beliefs: tuple[Belief, ...], source: SourceSnapshot) -> list[AddressRow]:
    by_axis = {belief.axis: belief for belief in beliefs}
    has_source = any(belief.capability is Capability.SOURCE for belief in beliefs)
    has_locality = any(belief.grade is Grade.LOCALITY and belief.members for belief in beliefs)
    if not has_source and not has_locality:
        return []
    rows: dict[str, AddressRow] = {}
    street = by_axis.get(Axis.STREET)
    folded_query = street.value if street else None
    if street is not None:
        async for batch in source.street_stream(
            folded_query, cap=STREET_STREAM_CAP, batch=STREET_BATCH
        ):
            for row in batch:
                rows.setdefault(row.address_id, row)
    for fetched in await asyncio.gather(
        *_recovery_fetches(by_axis, source, folded_query, CURRENT_LIFECYCLE)
    ):
        for row in fetched:
            rows.setdefault(row.address_id, row)
    return list(rows.values())


def _row(row: AddressRow, beliefs: tuple[Belief, ...], target_id: str | None) -> dict:
    return {
        "address_id": row.address_id,
        "is_match": row.address_id == target_id,
        "axes": {
            belief.axis.value: {
                "grade": belief.grade.value,
                "v": _belief_value(belief, row),
            }
            for belief in beliefs
        },
    }


def _record(
    target_id: str | None,
    mutations: list[str],
    beliefs: tuple[Belief, ...],
    rows: list[AddressRow],
    weights: dict,
    eps: float,
    top_rows: int,
    sample_rows: int,
    rng: random.Random,
) -> dict:
    candidates = [_row(row, beliefs, target_id) for row in rows]
    matched = [row for row in candidates if row["is_match"]]
    nonmatches = sorted(
        (row for row in candidates if not row["is_match"]), key=lambda row: row["address_id"]
    )
    top = sorted(nonmatches, key=lambda row: _score(row, weights, eps), reverse=True)[:top_rows]
    kept = sorted([*matched, *top], key=lambda row: row["address_id"])
    return {
        "target_id": target_id,
        "mutations": mutations,
        "rows": kept,
        "mu_nonmatch": rng.sample(nonmatches, min(sample_rows, len(nonmatches))),
    }


def _items(
    synth_path: str, limit: int, sample_seed: int
) -> list[tuple[str, str | None, list[str]]]:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    records = read_jsonl(synth_path)
    if len(records) > limit:
        records = random.Random(sample_seed).sample(records, limit)
    return [
        (
            record["raw"],
            record.get("target", {}).get("id") if record.get("target") else None,
            record.get("mutations") or [],
        )
        for record in records
    ]


async def _process(item, source, branches, weights, eps, top_rows, sample_rows, semaphore) -> dict:
    query, target_id, mutations = item
    async with semaphore:
        beliefs = _beliefs(query, branches)
        rows = await _pool(beliefs, source)
    return _record(
        target_id,
        mutations,
        beliefs,
        rows,
        weights,
        eps,
        top_rows,
        sample_rows,
        random.Random(query),
    )


async def _run(
    synth_path: str,
    out_path: str,
    weights: dict,
    eps: float,
    top_rows: int,
    sample_rows: int,
    concurrency: int,
    limit: int,
    sample_seed: int,
) -> tuple[str, int]:
    segmenter.load()
    settings = Settings()
    try:
        source = await PostgresAddressSource.connect(
            settings.database_dsn,
            host=settings.database_host,
            min_size=settings.db_pool_min,
            max_size=max(concurrency, settings.db_pool_min),
            refresh_interval=None,
            resolution_factory=build_resolution,
        )
    except NotSeeded:
        raise RuntimeError("database not seeded") from None
    items = _items(synth_path, limit, sample_seed)
    semaphore = asyncio.Semaphore(concurrency)
    print(f"[-] snapshotting {len(items)} queries -> {out_path}")
    completed = 0
    try:
        async with source.snapshot() as snap:
            generation = snap.generation
            tasks = [
                asyncio.create_task(
                    _process(
                        item,
                        snap,
                        snap.resolution.branches,
                        weights,
                        eps,
                        top_rows,
                        sample_rows,
                        semaphore,
                    )
                )
                for item in items
            ]
            with open(out_path, "w", encoding="utf-8") as output:  # noqa: ASYNC230
                for future in asyncio.as_completed(tasks):
                    output.write(json.dumps(await future) + "\n")
                    completed += 1
                    if completed % 500 == 0:
                        print(f"[-]   {completed}/{len(items)}")
    finally:
        await source.close()
    print(f"[+] wrote {completed} snapshot records")
    return generation, completed


def main() -> None:
    parser = argparse.ArgumentParser(description="build a synthetic score-calibration snapshot")
    parser.add_argument("--synth", default=str(_HARD))
    parser.add_argument("--out", required=True)
    parser.add_argument("--params", default=str(_PARAMS))
    parser.add_argument("--max-rows", type=int, default=200)
    parser.add_argument("--mu-rows", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--limit", type=int, default=2500)
    parser.add_argument("--sample-seed", type=int, default=1)
    args = parser.parse_args()
    params = json.loads(Path(args.params).read_text("utf-8"))
    generation, records = asyncio.run(
        _run(
            args.synth,
            args.out,
            params["weights"],
            params["eps"],
            args.max_rows,
            args.mu_rows,
            args.concurrency,
            args.limit,
            args.sample_seed,
        )
    )
    sidecar = Path(f"{args.out}.manifest.json")
    sidecar.write_text(
        json.dumps(
            manifest(
                files={"synth": args.synth},
                generation=generation,
                records=records,
                sample_seed=args.sample_seed,
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"[+] wrote {sidecar}")


if __name__ == "__main__":
    main()
