"""machine calibration: a frozen, deterministic proxy for the compute bifrost spends, so a bench
number measured here can be compared against one measured elsewhere.

three kernels, one per real bottleneck (dense SIMD, memory-bound scan, interpreter-bound scalar).
the index is the median of their ratios against a baked baseline, so `normalized = measured *
factor` and 1.0 means "as fast as the baking host". when the three ratios disagree the
host differs structurally from the baking host and the index is flagged untrusted, not averaged.

the kernels are FROZEN: they deliberately do not call engine code, because optimizing the engine
would silently move the reference. editing a kernel or how measure() aggregates invalidates every
baked baseline - bump KERNEL_VERSION.

widths are process counts, not client concurrency: the pinned configuration fixes server-side
parallelism (workers on cores), so one factor per width covers every concurrency point. bake at
width 1 (per-core speed, for latency) and width=workers (contention at the app's real width, for
throughput).

  taskset -c 0,2 python -m eval.calib --bake --widths 1,2 --label local-i5-2p --out baseline.json
  taskset -c 0,2 python -m eval.calib --baseline baseline.json
"""

from __future__ import annotations

import os

# blas must stay single-threaded or the kernels measure a thread pool instead of the machine
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_var, "1")

import argparse  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import random  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
from concurrent.futures import ProcessPoolExecutor  # noqa: E402
from multiprocessing import get_context  # noqa: E402
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402

from .provenance import manifest  # noqa: E402

KERNEL_VERSION = 1

# frozen kernel parameters; sized to land near 100ms each on a modern p-core
_SEED = 20260817
_GEMM_N, _GEMM_ITERS = 512, 45
_SCAN_N, _SCAN_HITS, _SCAN_TOP, _SCAN_ITERS = 4_000_000, 300_000, 200, 3
_SCALAR_PAIRS, _SCALAR_LEN = 3_600, 20

_REPEATS = 5
_MAX_SPREAD = 0.15  # repeat dispersion above this = host too noisy to publish from
_MAX_DIVERGENCE = 1.25  # kernel ratios this far apart = structurally different host, don't correct


def k_gemm() -> float:
    """dense float32 matmul; the segmenter's simd-bound onnx inference in generic form."""
    rng = np.random.default_rng(_SEED)
    a = rng.random((_GEMM_N, _GEMM_N), dtype=np.float32)
    b = rng.random((_GEMM_N, _GEMM_N), dtype=np.float32)
    t0 = time.perf_counter()
    for _ in range(_GEMM_ITERS):
        a @ b
    return time.perf_counter() - t0


def _scan_once(idx: np.ndarray, lens: np.ndarray, sim: np.ndarray) -> None:
    inter = np.bincount(idx, minlength=_SCAN_N)
    np.divide(inter, lens, out=sim)  # preallocated: a fresh 32mb output would measure page faults
    np.argpartition(sim, -_SCAN_TOP)[-_SCAN_TOP:]


def k_scan() -> float:
    """bincount over an inverted index, float64 divide, top-k: the trigram hot path's shape."""
    rng = np.random.default_rng(_SEED)
    idx = rng.integers(0, _SCAN_N, _SCAN_HITS, dtype=np.int64)
    lens = rng.random(_SCAN_N) + 1.0
    sim = np.empty(_SCAN_N, dtype=np.float64)
    _scan_once(idx, lens, sim)  # warm the allocator arenas; cold first-touch is 3x noisier
    t0 = time.perf_counter()
    for _ in range(_SCAN_ITERS):
        _scan_once(idx, lens, sim)
    return time.perf_counter() - t0


def _levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def k_scalar() -> float:
    """pure-python edit distance; gil-bound interpreter work, as the candidate scoring is."""
    rng = random.Random(_SEED)
    alpha = "abcdefghijklmnopqrstuvwxyz "
    pairs = [
        ("".join(rng.choices(alpha, k=_SCALAR_LEN)), "".join(rng.choices(alpha, k=_SCALAR_LEN)))
        for _ in range(_SCALAR_PAIRS)
    ]
    t0 = time.perf_counter()
    for a, b in pairs:
        _levenshtein(a, b)
    return time.perf_counter() - t0


KERNELS = {"gemm": k_gemm, "scan": k_scan, "scalar": k_scalar}


def _run(name: str) -> float:
    return KERNELS[name]()


def _pin(cpus: list[int] | None) -> None:
    # a child may widen past the parent's taskset mask, so the bench client can calibrate app cores
    if cpus:
        os.sched_setaffinity(0, set(cpus))


def measure(
    width: int, repeats: int = _REPEATS, cpus: list[int] | None = None
) -> dict[str, dict[str, float]]:
    """{kernel: {median, spread}} seconds for `width` concurrent copies of each kernel.

    the critical path of the batch (slowest copy, self-timed inside the kernel) - so pool spawn, IPC
    and input setup stay out of the number while contention between the copies stays in."""
    out: dict[str, dict[str, float]] = {}
    with ProcessPoolExecutor(
        max_workers=width, mp_context=get_context("spawn"), initializer=_pin, initargs=(cpus,)
    ) as ex:
        for name in KERNELS:
            times = []
            for r in range(repeats + 1):  # first pass warms the pool (spawn + numpy import)
                batch = list(ex.map(_run, [name] * width))
                if r:
                    times.append(max(batch))
            med = statistics.median(times)
            out[name] = {
                "median": round(med, 6),
                "spread": round((max(times) - min(times)) / med, 4),
            }
    return out


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text("utf-8").splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def host() -> dict:
    # affinity is the cpuset the run was pinned to; it is part of the identity of the measurement
    return {
        "cpu_model": _cpu_model(),
        "nproc": os.cpu_count(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "python": platform.python_version(),
        "numpy": np.__version__,
    }


def bake(widths: list[int], label: str, repeats: int = _REPEATS) -> dict:
    points = {}
    for w in widths:
        points[str(w)] = measure(w, repeats)
        shown = "  ".join(f"{k}={v['median'] * 1000:.1f}ms" for k, v in points[str(w)].items())
        print(f"[i] width {w}: {shown}")
    return manifest(
        kernel_version=KERNEL_VERSION,
        label=label,
        host=host(),
        repeats=repeats,
        points=points,
    )


def index(baseline: dict, now: dict[str, dict[str, float]], width: int) -> dict:
    """machine factor vs the baked host: normalized = measured * factor, 1.0 = same speed."""
    if baseline.get("kernel_version") != KERNEL_VERSION:
        return {
            "factor": None,
            "trusted": False,
            "reason": f"baseline kernel v{baseline.get('kernel_version')} != v{KERNEL_VERSION}",
        }
    baked = baseline.get("points", {}).get(str(width))
    if not baked:
        return {"factor": None, "trusted": False, "reason": f"no baked width {width}"}

    ratios = {k: baked[k]["median"] / now[k]["median"] for k in KERNELS}
    divergence = max(ratios.values()) / min(ratios.values())
    noisy = sorted(k for k, v in now.items() if v["spread"] > _MAX_SPREAD)
    reasons = []
    if noisy:
        reasons.append(f"repeat spread over {_MAX_SPREAD:.0%}: {noisy}")
    if divergence > _MAX_DIVERGENCE:
        reasons.append(f"kernel ratios diverge {divergence:.2f}x (limit {_MAX_DIVERGENCE})")
    return {
        "factor": round(statistics.median(ratios.values()), 4),
        "ratios": {k: round(v, 4) for k, v in ratios.items()},
        "divergence": round(divergence, 3),
        "width": width,
        "baseline_label": baseline.get("label"),
        "trusted": not reasons,
        "reason": "; ".join(reasons) or None,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="bake or check the machine calibration baseline")
    p.add_argument(
        "--bake", action="store_true", help="write a new baseline instead of checking one"
    )
    p.add_argument("--widths", default="1,2", help="process counts to calibrate (1,workers)")
    p.add_argument("--label", help="environment tag; required with --bake")
    p.add_argument("--baseline", help="baseline json to check this host against")
    p.add_argument("--out", help="where to write the baked baseline")
    p.add_argument("--repeats", type=int, default=_REPEATS)
    a = p.parse_args()
    widths = [int(w) for w in a.widths.split(",") if w.strip()]

    if a.bake:
        if not (a.label and a.out):
            raise SystemExit("[-] --bake needs --label and --out")
        doc = bake(widths, a.label, a.repeats)
        Path(a.out).write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
        print(f"[+] baked {a.out} (kernel v{KERNEL_VERSION}, widths {widths})")
        return

    if not a.baseline:
        raise SystemExit("[-] pass --baseline to check, or --bake to write one")
    baseline = json.loads(Path(a.baseline).read_text("utf-8"))
    for w in widths:
        got = index(baseline, measure(w, a.repeats), w)
        flag = "[+]" if got["trusted"] else "[!]"
        print(f"{flag} width {w}: factor={got['factor']} {got.get('reason') or ''}")


if __name__ == "__main__":
    main()
