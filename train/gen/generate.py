"""orchestrate: stream registry -> compose -> mutate -> labeled jsonl. pre-generated to disk.

emits a training corpus (tier mix weighted to the floor, `train` id-bucket) and a held-out hard set
(`held` id-bucket, intensity-swept past the observed noise rates). deterministic under --seed.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, replace
from pathlib import Path

from .address import Address, stream
from .compose import (
    _FIELD_SKIP_SHAPES,
    _FIELDS,
    FIELD_SKIP_WEIGHTS,
    P_FIELD_SKIP,
    P_PARTIAL,
    P_TYPED,
    Segment,
    _weighted,
    compose,
    partial_drop,
    prefix_cut,
    render,
    spans,
)
from .junk import junk_segment
from .mutate import DEFAULT, NoiseCfg, mutate, normalize_segs

# tier mix per split
_TRAIN_MIX = [(1, 0.70), (2, 0.22), (3, 0.08)]
_HARD_MIX = [(1, 0.34), (2, 0.33), (3, 0.33)]

# hard-set intensity sweep: 1.0 = base mix, larger values stack more mutations
_SWEEP = [1.0, 1.5, 2.0, 2.5, 3.0]
P_NO_TARGET = 0.16


def _target(a: Address, sp: list[dict]) -> dict:
    # target reflects only what the surface kept: null any component without a surviving span
    present = {s["label"] for s in sp}
    tgt = asdict(a)
    for f in _FIELDS:
        label = "street" if f == "street_name" else f
        if label not in present:
            tgt[f] = "" if f == "street_name" else None
    return tgt


def _invalidate(segs: list[Segment], kind: str, rng: random.Random) -> list[Segment]:
    label = "postcode" if kind == "invalid_postcode" else "house_number"
    value = "0000" if kind == "invalid_postcode" else str(rng.randint(10000, 99999))
    out = [replace(segment, text=value) if segment.label == label else segment for segment in segs]
    if any(segment.label == label for segment in out):
        return out
    return [*out, Segment(" ", None), Segment(value, label)]


def make_record(a: Address, tier: int, rng: random.Random, cfg: NoiseCfg = DEFAULT) -> dict:
    structural: list[str] = []
    targetable = not cfg.p_no_target or rng.random() >= cfg.p_no_target
    if targetable:
        negative_kind = None
        if cfg.p_partial and rng.random() < cfg.p_partial:
            p = partial_drop(a, rng)
            if p is not None:
                a = p
                structural.append("field_drop")
        elif cfg.p_field_skip and rng.random() < cfg.p_field_skip:
            p = partial_drop(a, rng, FIELD_SKIP_WEIGHTS, _FIELD_SKIP_SHAPES)
            if p is not None:
                a = p
                structural.append("field_skip")
        segs = compose(a, rng)
    else:
        negative_kind = (
            "junk_only"
            if rng.random() < 0.2
            else rng.choice(("invalid_postcode", "invalid_house_number"))
        )
        segs = [junk_segment(rng)] if negative_kind == "junk_only" else compose(a, rng)
        structural.append(negative_kind)
    segs, applied = mutate(segs, tier, rng, cfg)
    if cfg.p_typed and rng.random() < cfg.p_typed:  # final stage: truncate the mutated surface
        cut = prefix_cut(segs, rng)
        if render(cut) != render(segs):
            segs = cut
            applied.append("prefix_cut")
    if negative_kind and negative_kind != "junk_only":
        segs = _invalidate(segs, negative_kind, rng)
    noisy = render(segs)
    normalized_segs = normalize_segs(segs)
    normalized = render(normalized_segs)
    sp = spans(normalized_segs)
    mutations = [*structural, *applied]
    return {
        "raw": noisy,
        "normalized": normalized,
        "target": _target(a, sp) if targetable else None,
        "spans": sp,
        "tier": tier,
        "intensity": cfg.intensity,
        "noise_level": len(mutations),
        "mutations": mutations,
    }


def _reservoir(path: str, n: int, rng: random.Random, bucket: str | None = None) -> list[Address]:
    res: list[Address] = []
    for i, a in enumerate(stream(path, bucket)):
        if i < n:
            res.append(a)
        else:
            j = rng.randrange(i + 1)
            if j < n:
                res[j] = a
    return res


def _write(
    path: str, rows: list[Address], mix: list[tuple[int, float]], rng: random.Random, cfg: NoiseCfg
) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for a in rows:
            rec = make_record(a, _weighted(mix, rng), rng, cfg)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[+] wrote {len(rows)} records -> {path}")


def _write_hard(path: str, rows: list[Address], rng: random.Random, cfg: NoiseCfg) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for i, a in enumerate(rows):
            level = _SWEEP[i * len(_SWEEP) // len(rows)]  # equal-sized strata across the band
            rec = make_record(a, _weighted(_HARD_MIX, rng), rng, replace(cfg, intensity=level))
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[+] wrote {len(rows)} hard records (intensity {_SWEEP[0]}-{_SWEEP[-1]}) -> {path}")


def make_corpus(
    in_path: str,
    out_path: str,
    n: int,
    seed: int,
    cfg: NoiseCfg = DEFAULT,
    *,
    bucket: str = "train",
    mix: list[tuple[int, float]] = _TRAIN_MIX,
) -> None:
    """reservoir-sample one id-bucket and write a mutated corpus (the train-corpus writer)."""
    rng = random.Random(seed)
    rows = _reservoir(in_path, n, rng, bucket)
    rng.shuffle(rows)
    _write(out_path, rows, mix, rng, cfg)


def generate(
    in_path: str,
    out_path: str,
    hard_path: str,
    n: int,
    hard_frac: float,
    seed: int,
    cfg: NoiseCfg = DEFAULT,
) -> None:
    # leakage discipline: train/hard draw disjoint blake2b id-buckets; 2 passes over the registry
    print(f"[i] reservoir-sampling {n} train + {int(n * hard_frac)} hard from {in_path}")
    make_corpus(in_path, out_path, n, seed, cfg)
    rng = random.Random(seed)  # fresh stream for hard; train/held split is by id-bucket, not rng
    hard_rows = _reservoir(in_path, int(n * hard_frac), rng, "held")
    rng.shuffle(hard_rows)
    _write_hard(hard_path, hard_rows, rng, cfg)


def main() -> None:
    p = argparse.ArgumentParser(description="synthetic address-noise generator")
    p.add_argument("--in", dest="in_path", required=True)
    p.add_argument("--out", dest="out_path", required=True)
    p.add_argument("--hard-out", dest="hard_path", required=True)
    p.add_argument("--n", type=int, default=50000)
    p.add_argument("--hard-frac", type=float, default=0.15)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--intensity", type=float, default=1.0)
    p.add_argument(
        "--p-partial",
        dest="p_partial",
        type=float,
        default=P_PARTIAL,
        help="fraction of records emitted as structural partials (0 disables)",
    )
    p.add_argument(
        "--p-typed",
        dest="p_typed",
        type=float,
        default=P_TYPED,
        help="fraction of records truncated to a typed prefix (autocomplete; 0 disables)",
    )
    p.add_argument(
        "--p-field-skip",
        dest="p_field_skip",
        type=float,
        default=P_FIELD_SKIP,
        help="fraction reduced to street + a later field (autocomplete jump; 0 disables)",
    )
    p.add_argument(
        "--p-no-target",
        dest="p_no_target",
        type=float,
        default=P_NO_TARGET,
        help="fraction constructed without a valid registry target (0 disables)",
    )
    a = p.parse_args()
    cfg = NoiseCfg(
        intensity=a.intensity,
        p_partial=a.p_partial,
        p_field_skip=a.p_field_skip,
        p_typed=a.p_typed,
        p_no_target=a.p_no_target,
    )
    generate(a.in_path, a.out_path, a.hard_path, a.n, a.hard_frac, a.seed, cfg)


if __name__ == "__main__":
    main()
