"""closed-loop performance and accuracy benchmark over a generated held-out corpus."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .provenance import manifest
from .run import _CONFIDENT, _HARD, read_jsonl

_DATA_UPDATED = "X-Bifrost-Data-Updated"
_LOCAL = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})
_BATCH_CHUNK = 100


def _workload(path: str, limit: int | None = 2500, seed: int = 1) -> list[dict]:
    rows = read_jsonl(path)
    if limit is not None and len(rows) > limit:
        rows = random.Random(seed).sample(rows, limit)
    return [
        {
            "raw": row["raw"],
            "target_id": row.get("target", {}).get("id") if row.get("target") else None,
            "tier": row["tier"],
            "intensity": row["intensity"],
            "noise_level": row["noise_level"],
            "mutations": row.get("mutations") or [],
        }
        for row in rows
    ]


def _result_row(item: dict) -> dict:
    return {
        "tier": item["tier"],
        "intensity": item["intensity"],
        "noise_level": item["noise_level"],
        "mutations": item["mutations"],
        "target": item["target_id"] is not None,
        "status": 0,
        "result_count": 0,
        "rank": None,
        "confidence": None,
        "shed": False,
    }


def _http_error(row: dict, error: urllib.error.HTTPError) -> None:
    row["status"] = error.code
    try:
        detail = json.loads(error.read()).get("detail")
    except (json.JSONDecodeError, AttributeError):
        detail = None
    if error.code == 503 and detail == "overloaded":
        row["shed"] = True
        row["retry_after"] = error.headers.get("Retry-After")
    else:
        row["error"] = detail or error.reason
    error.close()


def _post(url: str, item: dict, timeout: float) -> dict:
    body = json.dumps({"query": item["raw"], "uuid": True}).encode()
    request = urllib.request.Request(url, body, {"Content-Type": "application/json"})
    row = _result_row(item)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
            row["status"] = response.status
            row["data_updated"] = response.headers.get(_DATA_UPDATED)
    except urllib.error.HTTPError as error:
        _http_error(row, error)
    except Exception as error:
        row["error"] = type(error).__name__
    else:
        matches = payload.get("matches") or []
        ids = [match["meta"].get("uuid") for match in matches]
        row["result_count"] = len(ids)
        row["confidence"] = matches[0]["meta"]["confidence"] if matches else None
        if item["target_id"] in ids:
            row["rank"] = ids.index(item["target_id"]) + 1
    row["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return row


def _pass(
    url: str, items: list[dict], concurrency: int, timeout: float
) -> tuple[list[dict], float]:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        rows = list(executor.map(lambda item: _post(url, item, timeout), items))
    return rows, time.perf_counter() - started


def _batch_pass(url: str, items: list[dict], timeout: float) -> dict:
    n = 0
    started = time.perf_counter()
    for i in range(0, len(items), _BATCH_CHUNK):
        chunk = [item["raw"] for item in items[i : i + _BATCH_CHUNK]]
        body = json.dumps({"query": chunk, "uuid": True}).encode()
        request = urllib.request.Request(url, body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read()
                n += len(chunk)
        except Exception as error:
            return {"error": type(error).__name__, "queries": n}
    seconds = time.perf_counter() - started
    return {
        "queries": n,
        "chunk": _BATCH_CHUNK,
        "ms_per_query": round(seconds * 1000 / max(n, 1), 3),
    }


def _pct(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return round(ordered[max(0, math.ceil(percentile / 100 * len(ordered)) - 1)], 3)


def _latency(rows: list[dict]) -> dict:
    values = [row["latency_ms"] for row in rows]
    return {
        "n": len(values),
        "p50": _pct(values, 50),
        "p95": _pct(values, 95),
        "p99": _pct(values, 99),
    }


def _accuracy(rows: list[dict]) -> dict:
    completed = [row for row in rows if row["status"] == 200]
    targeted = [row for row in completed if row["target"]]
    no_target = [row for row in completed if not row["target"]]
    reciprocal = sum(1 / row["rank"] for row in targeted if row["rank"])
    return {
        "targeted": {
            "n": len(targeted),
            "recall": {
                1: sum(row["rank"] == 1 for row in targeted) / max(len(targeted), 1),
                5: sum(bool(row["rank"] and row["rank"] <= 5) for row in targeted)
                / max(len(targeted), 1),
            },
            "mrr": reciprocal / max(len(targeted), 1),
        },
        "no_target": {
            "n": len(no_target),
            "return_rate": sum(row["result_count"] > 0 for row in no_target)
            / max(len(no_target), 1),
            "confident_rate": sum(
                row["result_count"] > 0 and row["confidence"] in _CONFIDENT for row in no_target
            )
            / max(len(no_target), 1),
        },
    }


def _breakdown(rows: list[dict], key: str) -> dict:
    groups: dict[object, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row[key]].append(row)
    return {str(value): _accuracy(group) for value, group in sorted(groups.items())}


def _mutation_breakdown(rows: list[dict]) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        for mutation in row["mutations"] or ["none"]:
            groups[mutation].append(row)
    return {name: _accuracy(group) for name, group in sorted(groups.items())}


def _summary(rows: list[dict], seconds: float) -> dict[str, Any]:
    completed = [row for row in rows if row["status"] == 200]
    shed = [row for row in rows if row["shed"]]
    tiers: dict[int, list[dict]] = defaultdict(list)
    for row in completed:
        tiers[row["tier"]].append(row)
    return {
        **_latency(completed),
        "requests": len(rows),
        "seconds": round(seconds, 3),
        "rps": round(len(completed) / seconds, 1) if seconds else 0.0,
        "response_rps": round(len(rows) / seconds, 1) if seconds else 0.0,
        "shedding": {
            "n": len(shed),
            "rate": len(shed) / max(len(rows), 1),
            "latency": _latency(shed),
        },
        "errors": dict(
            Counter(
                row.get("error") or row["status"]
                for row in rows
                if row["status"] != 200 and not row["shed"]
            )
        ),
        "empty": sum(row["status"] == 200 and row["result_count"] == 0 for row in rows),
        "accuracy": _accuracy(rows),
        "latency_by_tier": {str(tier): _latency(group) for tier, group in sorted(tiers.items())},
        "accuracy_by_tier": _breakdown(rows, "tier"),
        "accuracy_by_intensity": _breakdown(rows, "intensity"),
        "accuracy_by_noise_level": _breakdown(rows, "noise_level"),
        "accuracy_by_mutation": _mutation_breakdown(rows),
    }


def _require_success(label: str, summary: dict, *, allow_shedding: bool = False) -> None:
    if summary["errors"]:
        raise SystemExit(f"[!] {label} failed: {summary['errors']}")
    if summary["shedding"]["n"] and not allow_shedding:
        raise SystemExit(f"[!] {label} shed {summary['shedding']['n']} requests")


def _normalize(summary: dict, factor: float) -> dict:
    out = {f"p{p}": round(summary[f"p{p}"] * factor, 3) for p in (50, 95, 99)}
    out["rps"] = round(summary["rps"] / factor, 1)
    out["latency_by_tier"] = {
        tier: {f"p{p}": round(stats[f"p{p}"] * factor, 3) for p in (50, 95, 99)}
        for tier, stats in summary["latency_by_tier"].items()
    }
    return out


def _rss_mib(pid: str) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text("utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def _worker_rss(pattern: str) -> dict:
    children: dict[str, list[str]] = defaultdict(list)
    roots = []
    for pid in (path for path in os.listdir("/proc") if path.isdigit()):
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            ppid = Path(f"/proc/{pid}/stat").read_text("utf-8").rsplit(")", 1)[1].split()[1]
        except (OSError, IndexError):
            continue
        children[ppid].append(pid)
        if pattern in command:
            roots.append(pid)
    seen, queue = {}, list(roots)
    while queue:
        pid = queue.pop()
        if pid in seen:
            continue
        if (mib := _rss_mib(pid)) is not None:
            seen[pid] = mib
        queue.extend(children.get(pid, ()))
    return {"pids": len(seen), "mib": sorted(seen.values(), reverse=True)}


def _is_local(base_url: str) -> bool:
    return (urllib.parse.urlparse(base_url).hostname or "") in _LOCAL


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def _measure_calibration(
    path: str | None, workers: int, app_cpus: list[int]
) -> tuple[dict | None, dict | None]:
    if path is None:
        return None, None
    from . import calib

    baseline = json.loads(Path(path).read_text("utf-8"))
    latency = calib.index(baseline, calib.measure(1, cpus=app_cpus), 1)
    throughput = calib.index(baseline, calib.measure(workers, cpus=app_cpus), workers)
    print(f"[i] index  latency={latency['factor']} throughput={throughput['factor']}")
    if not (latency["trusted"] and throughput["trusted"]):
        print(f"[!] calibration untrusted: {latency['reason']} / {throughput['reason']}")
    return latency, throughput


def main() -> None:
    parser = argparse.ArgumentParser(description="closed-loop /resolve synthetic benchmark")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--synth", default=str(_HARD))
    parser.add_argument("--out", required=True)
    parser.add_argument("--concurrency", default="1,4,16,160")
    parser.add_argument("--limit", type=int, default=2500)
    parser.add_argument("--sample-seed", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--cache-state", required=True, choices=("cold", "warm", "disabled"))
    parser.add_argument("--calibration")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--app-cpus", default="")
    parser.add_argument("--rss-match", default="bifrost.api.main")
    args = parser.parse_args()

    if args.calibration and not _is_local(args.base_url):
        raise SystemExit("[-] --calibration measures this host; it is meaningless against a remote")

    url = args.base_url.rstrip("/") + "/resolve"
    items = _workload(args.synth, args.limit, args.sample_seed)
    if not items:
        raise SystemExit(f"[-] no records in {args.synth}")
    schedule = [int(value) for value in args.concurrency.split(",") if value.strip()]
    app_cpus = [int(value) for value in args.app_cpus.split(",") if value.strip()]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    targeted = sum(item["target_id"] is not None for item in items)
    tiers = dict(Counter(item["tier"] for item in items))
    print(f"[i] {len(items)} queries, {targeted} targeted, tiers={tiers}")

    latency, throughput = _measure_calibration(args.calibration, args.workers, app_cpus)

    print(f"[-] warming {url} with {len(items)} sequential requests")
    warm_rows, warm_seconds = _pass(url, items, 1, args.timeout)
    warm = _summary(warm_rows, warm_seconds)
    print(f"[i] warm  p50={warm['p50']}ms rps={warm['rps']} errors={warm['errors']}")
    _require_success("warm pass", warm)

    runs, rss = [], _worker_rss(args.rss_match)
    for concurrency in schedule:
        rows, seconds = _pass(url, items, concurrency, args.timeout)
        summary: dict[str, Any] = _summary(rows, seconds) | {"concurrency": concurrency}
        _require_success(f"concurrency {concurrency}", summary, allow_shedding=True)
        _write(out_dir / f"results-c{concurrency}.jsonl", rows)
        runs.append(summary)
        peak = _worker_rss(args.rss_match)
        rss = peak if sum(peak["mib"]) > sum(rss["mib"]) else rss
        accuracy = summary["accuracy"]["targeted"]["recall"]
        print(
            f"[i] c={concurrency:<4d} rps={summary['rps']} p50={summary['p50']}ms "
            f"p99={summary['p99']}ms r@1={accuracy[1]:.3f} r@5={accuracy[5]:.3f} "
            f"shed={summary['shedding']['n']} errors={summary['errors']}"
        )

    batch = _batch_pass(url, items, args.timeout)
    calibration = None
    if latency and throughput:
        calibration = {"latency": latency, "throughput": throughput}
        for run in runs:
            factor = latency["factor"] if run["concurrency"] == 1 else throughput["factor"]
            if factor:
                run["normalized"] = _normalize(run, factor)
        if latency["factor"]:
            warm["normalized"] = _normalize(warm, latency["factor"])
        if throughput["factor"] and "ms_per_query" in batch:
            batch["normalized_ms_per_query"] = round(
                batch["ms_per_query"] * throughput["factor"], 3
            )

    served = next((row.get("data_updated") for row in warm_rows if row.get("data_updated")), None)
    output = manifest(
        files={"synth": args.synth, "calibration": args.calibration},
        base_url=args.base_url,
        n=len(items),
        workload="deterministic sample of the generated held-out corpus",
        sample_seed=args.sample_seed,
        data_updated=served,
        cache_state=args.cache_state,
        concurrency=schedule,
        config={
            "workers": args.workers,
            "app_cpus": app_cpus,
            "client_affinity": sorted(os.sched_getaffinity(0)),
        },
        calibration=calibration,
        worker_rss=rss,
        warm=warm,
        batch=batch,
        runs=runs,
    )
    (out_dir / "manifest.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"[+] wrote {out_dir}/manifest.json + {len(runs)} result files")


if __name__ == "__main__":
    main()
