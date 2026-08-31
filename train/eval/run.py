"""resolver recall and segment F1 over a generated held-out corpus."""

from __future__ import annotations

import argparse
import importlib
import json
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from bifrost.core.types import TOP_K

_DATA = Path(__file__).resolve().parent.parent / "data"
_HARD = _DATA / "synth" / "hard.jsonl"
_KS = (1, TOP_K)
_CONFIDENT = frozenset({"A", "B"})


@dataclass(frozen=True, slots=True)
class Resolution:
    ids: list[str]
    category: str | None = None


Resolver = Callable[[str], Resolution]
Segmenter = Callable[[str], list[tuple[str, int, int]]]


def confident(res: Resolution) -> bool:
    return bool(res.ids) and res.category in _CONFIDENT


def _prf(tp: int, pred: int, expected: int) -> tuple[float, float, float]:
    precision = tp / pred if pred else 0.0
    recall = tp / expected if expected else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


def span_f1(
    pred: set[tuple[str, int, int]], expected: set[tuple[str, int, int]]
) -> tuple[float, float, float]:
    return _prf(len(pred & expected), len(pred), len(expected))


def spanset(spans: list[dict]) -> set[tuple[str, int, int]]:
    return {(s["label"], s["start"], s["end"]) for s in spans}


@dataclass(slots=True)
class _Acc:
    n: int = 0
    hits: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    rr: float = 0.0

    def add(self, answer: str, res: Resolution) -> None:
        self.n += 1
        rank = res.ids.index(answer) + 1 if answer in res.ids else None
        for k in _KS:
            self.hits[k] += int(rank is not None and rank <= k)
        if rank is not None:
            self.rr += 1 / rank

    def report(self) -> dict:
        n = max(self.n, 1)
        return {
            "n": self.n,
            "recall": {k: self.hits[k] / n for k in _KS},
            "mrr": self.rr / n,
        }


def _target_id(rec: dict) -> str | None:
    target = rec.get("target")
    return target.get("id") if target else None


def _resolver_metrics(records: Iterable[dict], resolve: Resolver) -> dict:
    overall = _Acc()
    by_tier: dict[int, _Acc] = defaultdict(_Acc)
    by_intensity: dict[float, _Acc] = defaultdict(_Acc)
    by_noise: dict[int, _Acc] = defaultdict(_Acc)
    by_mutation: dict[str, _Acc] = defaultdict(_Acc)
    no_target_n = no_target_returned = no_target_confident = 0

    for rec in records:
        res = resolve(rec["raw"])
        answer = _target_id(rec)
        if answer is None:
            no_target_n += 1
            no_target_returned += int(bool(res.ids))
            no_target_confident += int(confident(res))
            continue
        overall.add(answer, res)
        by_tier[rec["tier"]].add(answer, res)
        by_intensity[rec["intensity"]].add(answer, res)
        by_noise[rec["noise_level"]].add(answer, res)
        names = rec.get("mutations") or ["none"]
        for name in names:
            by_mutation[name].add(answer, res)

    return {
        "targeted": {
            "overall": overall.report(),
            "by_tier": {k: v.report() for k, v in sorted(by_tier.items())},
            "by_intensity": {k: v.report() for k, v in sorted(by_intensity.items())},
            "by_noise_level": {k: v.report() for k, v in sorted(by_noise.items())},
            "by_mutation": {k: v.report() for k, v in sorted(by_mutation.items())},
        },
        "no_target": {
            "n": no_target_n,
            "returned": no_target_returned,
            "return_rate": no_target_returned / max(no_target_n, 1),
            "confident": no_target_confident,
            "confident_rate": no_target_confident / max(no_target_n, 1),
        },
    }


def _segmenter_metrics(records: Iterable[dict], segment: Segmenter) -> dict:
    micro = [0, 0, 0]
    by_label: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for rec in records:
        expected = spanset(rec["spans"])
        pred = set(segment(rec.get("normalized", rec["raw"])))
        correct = pred & expected
        micro[0] += len(correct)
        micro[1] += len(pred)
        micro[2] += len(expected)
        for span in pred:
            by_label[span[0]][1] += 1
        for span in expected:
            by_label[span[0]][2] += 1
        for span in correct:
            by_label[span[0]][0] += 1
    precision, recall, f1 = _prf(*micro)
    return {
        "micro": {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "spans": micro[2],
        },
        "by_label": {
            label: dict(zip(("precision", "recall", "f1"), _prf(*counts), strict=True))
            for label, counts in sorted(by_label.items())
        },
    }


def score(records: Iterable[dict], resolver: Resolver | None, segmenter: Segmenter | None) -> dict:
    rows = list(records)
    out: dict = {}
    if resolver is not None:
        out["resolver"] = _resolver_metrics(rows, resolver)
    if segmenter is not None:
        out["segmenter"] = _segmenter_metrics(rows, segmenter)
    return out


def read_jsonl(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def evaluate(
    resolver: Resolver | None, segmenter: Segmenter | None, synth_path: str | Path
) -> dict:
    return score(read_jsonl(synth_path), resolver, segmenter)


def _load(spec: str) -> Callable:
    module, _, attr = spec.partition(":")
    if not attr:
        raise SystemExit(f"[-] bad spec {spec!r}; want 'pkg.module:callable'")
    return getattr(importlib.import_module(module), attr)


def _fmt(metrics: dict) -> str:
    recall = "  ".join(f"r@{k}={metrics['recall'][k]:.3f}" for k in _KS)
    return f"n={metrics['n']:>6d}  {recall}  mrr={metrics['mrr']:.3f}"


def print_report(report: dict) -> None:
    resolver = report.get("resolver")
    if resolver:
        targeted = resolver["targeted"]
        print("\n[i] resolver")
        print(f"  overall       {_fmt(targeted['overall'])}")
        for tier, metrics in targeted["by_tier"].items():
            print(f"    tier {tier}      {_fmt(metrics)}")
        no_target = resolver["no_target"]
        print(
            f"  no target     n={no_target['n']}  returned={no_target['return_rate']:.1%}  "
            f"confident={no_target['confident_rate']:.1%}"
        )
    segmenter = report.get("segmenter")
    if segmenter:
        metrics = segmenter["micro"]
        print(
            f"\n[i] segmenter  f1={metrics['f1']:.3f} "
            f"(p={metrics['precision']:.3f} r={metrics['recall']:.3f}, spans={metrics['spans']})"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="evaluation over a generated held-out corpus")
    parser.add_argument("--resolver", help="pkg.module:callable (raw -> Resolution)")
    parser.add_argument("--segmenter", help="pkg.module:callable (normalized -> spans)")
    parser.add_argument("--synth", default=str(_HARD))
    parser.add_argument("--json", dest="json_out", help="write the full report here")
    args = parser.parse_args()
    if not args.resolver and not args.segmenter:
        raise SystemExit("[-] nothing to evaluate: pass --resolver and/or --segmenter")
    resolver = _load(args.resolver) if args.resolver else None
    segmenter = _load(args.segmenter) if args.segmenter else None
    report = evaluate(resolver, segmenter, args.synth)
    print_report(report)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\n[+] wrote {args.json_out}")


if __name__ == "__main__":
    main()
