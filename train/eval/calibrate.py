"""fit ranking weights and confidence margins from synthetic candidate snapshots."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from bifrost.core.types import TOP_K

from .provenance import manifest
from .run import _CONFIDENT, read_jsonl

_AXES = (
    "street",
    "house_number",
    "house_letter",
    "floor",
    "door",
    "postcode",
    "city",
    "sub_locality",
)
_KS = (1, TOP_K)
_ALPHA = 1.0
_FACTORS = (0.4, 0.6, 0.8, 1.25, 1.6, 2.5)
_AGREE_TAU = {"trigram": 0.5, "husnr_fuzzy": 0.99, "postcode_fuzzy": 0.99}
_CORRECTION_MUTATIONS = frozenset(
    {
        "abbreviate",
        "chaos",
        "duplicate",
        "field_drop",
        "field_skip",
        "junk_prefix",
        "junk_suffix",
        "multi_junk",
        "ocr",
        "prefix_cut",
        "reorder",
        "swap_postcode_city",
        "token_merge",
        "token_split",
        "transpose",
        "truncate",
        "typo",
        "unit_notation",
    }
)


def _agree(grade: str, value: float) -> float:
    threshold = _AGREE_TAU.get(grade)
    return float(value >= threshold) if threshold is not None else value


def _accumulate(
    aggregate: dict[str, dict[str, list[float]]],
    grades: dict[str, str],
    side: str,
    axes: dict,
) -> None:
    for axis, grade in axes.items():
        cell = aggregate[axis][side]
        cell[0] += _agree(grade["grade"], grade["v"])
        cell[1] += grade["v"]
        cell[2] += 1
        grades[axis] = grade["grade"]


def _mu(records) -> tuple[dict[str, tuple[float, float]], dict[str, tuple[float, float, float]]]:
    aggregate = {axis: {"m": [0.0, 0.0, 0.0], "u": [0.0, 0.0, 0.0]} for axis in _AXES}
    grades = dict.fromkeys(_AXES, "")  # last grade seen per axis; selects the diagnostics tau
    for record in records:
        for row in record["rows"]:
            if row["is_match"]:
                _accumulate(aggregate, grades, "m", row["axes"])
        for row in record["mu_nonmatch"]:
            _accumulate(aggregate, grades, "u", row["axes"])
    mu, diagnostics = {}, {}
    for axis, data in aggregate.items():
        if data["m"][2] == 0 or data["u"][2] == 0:
            continue
        m = (data["m"][0] + _ALPHA) / (data["m"][2] + 2 * _ALPHA)
        u = (data["u"][0] + _ALPHA) / (data["u"][2] + 2 * _ALPHA)
        mu[axis] = (m, u)
        if grades[axis] in _AGREE_TAU:
            diagnostics[axis] = (
                data["m"][1] / data["m"][2],
                data["u"][1] / data["u"][2],
                _AGREE_TAU[grades[axis]],
            )
    return mu, diagnostics


def _fs_weights(mu, eps) -> dict[str, float]:
    scale = math.log(1.0 / eps)
    return {
        axis: max(0.0, (math.log(m / u) - math.log((1 - m) / (1 - u))) / scale)
        for axis, (m, u) in mu.items()
    }


def _score(row, weights, eps) -> float:
    total = 0.0
    for axis, grade in row["axes"].items():
        weight = weights.get(axis, 0.0)
        if not weight:
            continue
        total += (
            weight * grade["v"] if grade["grade"] == "unit" else weight * math.log(eps + grade["v"])
        )
    return total


def _ranked(record, weights, eps) -> list[dict]:
    return sorted(record["rows"], key=lambda row: _score(row, weights, eps), reverse=True)


def _metrics(records, weights, eps) -> dict:
    targeted = [record for record in records if record.get("target_id")]
    hits = {k: 0 for k in _KS}
    reciprocal = 0.0
    for record in targeted:
        ids = [row["address_id"] for row in _ranked(record, weights, eps)]
        if record["target_id"] not in ids:
            continue
        rank = ids.index(record["target_id"])
        reciprocal += 1.0 / (rank + 1)
        for k in _KS:
            hits[k] += int(rank < k)
    n = max(len(targeted), 1)
    return {"recall": {k: hits[k] / n for k in _KS}, "mrr": reciprocal / n}


def _objective(metrics) -> tuple[float, float]:
    return metrics["recall"][TOP_K], metrics["mrr"]


def _optimize(records, seed_weights, eps, passes=3):
    weights = dict(seed_weights)
    best = _objective(_metrics(records, weights, eps))
    for _ in range(passes):
        improved = False
        for axis in list(weights):
            base = weights[axis]
            for factor in _FACTORS:
                trial = dict(weights)
                trial[axis] = base * factor
                objective = _objective(_metrics(records, trial, eps))
                if objective > best:
                    best, weights[axis], improved = objective, trial[axis], True
        if not improved:
            break
    return weights, best


def _gap(record, weights, eps) -> tuple[float, int]:
    scores = sorted((_score(row, weights, eps) for row in record["rows"]), reverse=True)
    return (scores[0] - scores[1] if len(scores) >= 2 else 0.0), len(scores)


def _leader_category(gap: float, n: int, margin_a: float, margin_b: float) -> str:
    if n == 0:
        return "C"
    if n == 1:
        return "B"
    if gap < margin_b:
        return "C"
    return "A" if gap >= margin_a else "B"


def _expected_category(record, weights, eps) -> str:
    ranked = _ranked(record, weights, eps)
    target_id = record.get("target_id")
    if target_id is None or not ranked or ranked[0]["address_id"] != target_id:
        return "C"
    if set(record.get("mutations") or ()) & _CORRECTION_MUTATIONS:
        return "B"
    return "A"


def _sim_confidence(prepared, margin_a: float, margin_b: float) -> tuple[float, int]:
    correct = false_confident = 0
    for expected, gap, n in prepared:
        predicted = _leader_category(gap, n, margin_a, margin_b)
        correct += int(predicted == expected)
        false_confident += int(expected == "C" and predicted in _CONFIDENT)
    return correct / max(len(prepared), 1), false_confident


def _fit_margins(records, weights, eps, ref_a, ref_b):
    prepared = [
        (_expected_category(record, weights, eps), *_gap(record, weights, eps))
        for record in records
    ]
    if sum(expected in _CONFIDENT for expected, *_ in prepared) < 40:
        return None
    _, reference_false = _sim_confidence(prepared, ref_a, ref_b)
    candidates = sorted({round(gap, 4) for _, gap, _ in prepared})
    candidates = candidates[:: max(1, len(candidates) // 200)]
    best = None
    for margin_b in candidates:
        for margin_a in candidates:
            if margin_a < margin_b:
                continue
            accuracy, false_confident = _sim_confidence(prepared, margin_a, margin_b)
            if false_confident <= reference_false:
                key = accuracy, -false_confident
                if best is None or key > best[0]:
                    best = key, margin_a, margin_b
    if best is None:
        return None
    (accuracy, negative_false), margin_a, margin_b = best
    false_confident = -negative_false
    n = len(prepared)
    return (
        margin_a,
        margin_b,
        {
            "category_accuracy": accuracy,
            "false_confident_rate": false_confident / max(n, 1),
            "records": n,
        },
    )


def _calibrate_weights(records, base, eps, no_optimize):
    if not any(record.get("target_id") for record in records):
        raise SystemExit("[!] snapshot has no target-bearing records")
    mu, diagnostics = _mu(records)
    fs_weights = _fs_weights(mu, eps)
    print("[i] axis            m      u      w_hand   w_fs")
    for axis in _AXES:
        if axis in mu:
            m, u = mu[axis]
            print(
                f"    {axis:14s} {m:.3f}  {u:.3f}  "
                f"{base['weights'].get(axis, 0):6.3f}  {fs_weights[axis]:6.3f}"
            )
    for axis, (match_mean, nonmatch_mean, threshold) in diagnostics.items():
        print(
            f"[i]   {axis} raw grade mean match={match_mean:.3f} "
            f"non-match={nonmatch_mean:.3f} (agree tau={threshold})"
        )
    weights = fs_weights
    if not no_optimize:
        weights, best = _optimize(records, fs_weights, eps)
        print(f"[+] optimized recall@{TOP_K}={best[0]:.3f} mrr={best[1]:.3f}")
    return weights


def main() -> None:
    parser = argparse.ArgumentParser(description="synthetic score and confidence calibration")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--out", default="../app/src/bifrost/db/artifacts/score_params.json")
    parser.add_argument("--no-optimize", action="store_true")
    parser.add_argument("--keep-weights", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()

    provenance = manifest(
        files={"snapshot": args.snapshot},
        source="synthetic fellegi-sunter calibration",
        weights="kept" if args.keep_weights else "fitted",
        alpha=_ALPHA,
        thresholds=_AGREE_TAU,
    )
    if provenance["git_dirty"] and not args.allow_dirty:
        raise SystemExit("[!] tree is dirty: commit first so the artifact git_sha is reproducible")
    base = json.loads(Path(args.out).read_text("utf-8"))
    eps = base["eps"]
    if not 0 < eps < 1:
        raise SystemExit(f"[!] eps must be in (0,1); got {eps}")
    records = read_jsonl(args.snapshot)
    weights = (
        dict(base["weights"])
        if args.keep_weights
        else _calibrate_weights(records, base, eps, args.no_optimize)
    )
    fit = _fit_margins(records, weights, eps, base["margins"]["a"], base["margins"]["b"])
    margins = dict(base["margins"])
    if fit is not None:
        margin_a, margin_b, report = fit
        margins = {"a": round(margin_a, 4), "b": round(margin_b, 4)}
        print(
            f"[+] margins a={margins['a']} b={margins['b']}: "
            f"category-accuracy={report['category_accuracy']:.3f} "
            f"false-confident={report['false_confident_rate']:.1%}"
        )

    provenance["margins_fit"] = fit is not None
    sidecar = Path(f"{args.snapshot}.manifest.json")
    if sidecar.is_file():
        provenance["snapshot_run"] = json.loads(sidecar.read_text("utf-8"))
    output = {
        "eps": eps,
        "weights": {
            axis: round(weights.get(axis, base["weights"].get(axis, 0.0)), 4) for axis in _AXES
        },
        "margins": margins,
        "provenance": provenance,
    }
    Path(args.out).write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"[+] wrote {args.out}")


if __name__ == "__main__":
    main()
