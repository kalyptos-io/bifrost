"""calibration index arithmetic and benchmark workload checks."""

from __future__ import annotations

import json
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError

import numpy as np
import pytest

from . import calib
from .bench import (
    _http_error,
    _is_local,
    _normalize,
    _require_success,
    _summary,
    _workload,
)


def _point(gemm: float, scan: float, scalar: float, spread: float = 0.01) -> dict:
    return {
        k: {"median": v, "spread": spread}
        for k, v in (("gemm", gemm), ("scan", scan), ("scalar", scalar))
    }


def _baseline(point: dict, version: int = calib.KERNEL_VERSION) -> dict:
    return {"kernel_version": version, "label": "test", "points": {"1": point}}


def test_kernels_do_a_fixed_amount_of_work() -> None:
    # the kernels return elapsed time, so "deterministic" means the work, not the duration
    a = np.random.default_rng(calib._SEED).random((calib._GEMM_N, calib._GEMM_N), dtype=np.float32)
    b = np.random.default_rng(calib._SEED).random((calib._GEMM_N, calib._GEMM_N), dtype=np.float32)
    assert np.array_equal(a, b)
    assert calib._levenshtein("rosenvaengets alle", "rosenvengets alle") == 1
    assert calib._levenshtein("", "abc") == 3


def test_index_expresses_this_host_in_the_baked_hosts_terms() -> None:
    baked = _point(0.1, 0.1, 0.1)
    got = calib.index(_baseline(baked), _point(0.2, 0.2, 0.2), 1)  # this host is 2x slower
    assert got["factor"] == 0.5 and got["trusted"]
    same = calib.index(_baseline(baked), _point(0.1, 0.1, 0.1), 1)
    assert same["factor"] == 1.0


def test_median_of_ratios_ignores_one_odd_kernel() -> None:
    baked = _point(0.1, 0.1, 0.1)
    got = calib.index(_baseline(baked), _point(0.1, 0.11, 0.1), 1)
    assert got["factor"] == 1.0 and got["trusted"]  # divergence 1.1x, under the limit


def test_noisy_repeats_are_untrusted() -> None:
    baked = _point(0.1, 0.1, 0.1)
    got = calib.index(_baseline(baked), _point(0.1, 0.1, 0.1, spread=0.4), 1)
    assert not got["trusted"] and "spread" in got["reason"]


def test_structurally_different_host_is_untrusted() -> None:
    baked = _point(0.1, 0.1, 0.1)
    got = calib.index(_baseline(baked), _point(0.05, 0.2, 0.1), 1)  # 4x apart across kernels
    assert not got["trusted"] and "diverge" in got["reason"]


def test_stale_baseline_is_rejected() -> None:
    got = calib.index(_baseline(_point(0.1, 0.1, 0.1), version=99), _point(0.1, 0.1, 0.1), 1)
    assert got["factor"] is None and not got["trusted"]
    missing = calib.index(_baseline(_point(0.1, 0.1, 0.1)), _point(0.1, 0.1, 0.1), 8)
    assert missing["factor"] is None and not missing["trusted"]


def test_workload_reads_generator_records(tmp_path) -> None:
    recs = [
        {
            "raw": "a",
            "target": {"id": "id-a"},
            "tier": 1,
            "intensity": 1.0,
            "noise_level": 0,
            "mutations": [],
        },
        {
            "raw": "b",
            "target": None,
            "tier": 3,
            "intensity": 2.0,
            "noise_level": 1,
            "mutations": ["invalid_postcode"],
        },
    ]
    path = tmp_path / "held.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    assert _workload(str(path), limit=None) == [
        {
            "raw": "a",
            "target_id": "id-a",
            "tier": 1,
            "intensity": 1.0,
            "noise_level": 0,
            "mutations": [],
        },
        {
            "raw": "b",
            "target_id": None,
            "tier": 3,
            "intensity": 2.0,
            "noise_level": 1,
            "mutations": ["invalid_postcode"],
        },
    ]


def test_normalize_moves_latency_and_throughput_opposite_ways() -> None:
    summary = {
        "p50": 10.0,
        "p95": 20.0,
        "p99": 30.0,
        "rps": 100.0,
        "latency_by_tier": {},
    }
    out = _normalize(summary, 0.5)  # this host twice as fast as the baked one
    assert (out["p50"], out["p99"]) == (5.0, 15.0)
    assert out["rps"] == 200.0


def test_calibration_refuses_a_remote_host() -> None:
    assert _is_local("http://localhost:8000") and _is_local("http://127.0.0.1:8000")
    assert not _is_local("https://bifrost.example.com")


def test_benchmark_rejects_request_errors() -> None:
    with pytest.raises(SystemExit, match="warm pass failed"):
        _require_success("warm pass", {"errors": {"URLError": 2}, "shedding": {"n": 0}})


def test_benchmark_reports_overload_shedding_separately() -> None:
    headers = Message()
    headers["Retry-After"] = "1"
    error = HTTPError(
        "http://localhost:8000/resolve",
        503,
        "Service Unavailable",
        headers,
        BytesIO(b'{"detail":"overloaded"}'),
    )
    row = {"status": 0, "shed": False}

    _http_error(row, error)

    assert row == {"status": 503, "shed": True, "retry_after": "1"}


def test_summary_excludes_shed_requests_from_accuracy_and_processing_rate() -> None:
    base = {
        "tier": 1,
        "intensity": 1.0,
        "noise_level": 0,
        "mutations": [],
        "target": True,
        "result_count": 1,
        "confidence": "A",
        "latency_ms": 10.0,
        "error": None,
    }
    completed = base | {"status": 200, "rank": 1, "shed": False}
    shed = base | {"status": 503, "rank": None, "shed": True}

    summary = _summary([completed, shed], 1.0)

    assert summary["rps"] == 1.0 and summary["response_rps"] == 2.0
    assert summary["shedding"]["n"] == 1 and summary["errors"] == {}
    assert summary["accuracy"]["targeted"]["n"] == 1
    _require_success("load", summary, allow_shedding=True)
