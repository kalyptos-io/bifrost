"""self-checks for synthetic score calibration."""

from __future__ import annotations

from .calibrate import (
    _agree,
    _fit_margins,
    _fs_weights,
    _leader_category,
    _metrics,
    _mu,
    _objective,
    _optimize,
    _sim_confidence,
)

_EPS = 0.001


def _pool(target_id: str, match: dict, nonmatch: dict, n_nonmatch: int = 3) -> dict:
    nonmatches = [
        {"address_id": f"{target_id}-n{i}", "is_match": False, "axes": nonmatch}
        for i in range(n_nonmatch)
    ]
    rows = [{"address_id": target_id, "is_match": True, "axes": match}, *nonmatches]
    return {
        "target_id": target_id,
        "mutations": [],
        "rows": rows,
        "mu_nonmatch": nonmatches,
    }


def _mu_records(n: int = 40) -> list[dict]:
    records = []
    for i in range(n):
        locality = float(i % 2)
        match = {
            "street": {"grade": "trigram", "v": 0.9},
            "house_number": {"grade": "husnr_fuzzy", "v": 1.0},
            "city": {"grade": "locality", "v": locality},
        }
        nonmatch = {
            "street": {"grade": "trigram", "v": 0.2},
            "house_number": {"grade": "husnr_fuzzy", "v": 0.0},
            "city": {"grade": "locality", "v": locality},
        }
        records.append(_pool(f"target-{i}", match, nonmatch))
    return records


def test_agree_binarizes_continuous_grades() -> None:
    assert _agree("trigram", 0.9) == 1.0 and _agree("trigram", 0.2) == 0.0
    assert _agree("husnr_fuzzy", 1.0) == 1.0 and _agree("husnr_fuzzy", 0.5) == 0.0
    assert _agree("exact", 1.0) == 1.0 and _agree("exact", 0.0) == 0.0


def test_mu_probabilities_and_weights() -> None:
    mu, diagnostics = _mu(_mu_records())
    for axis, (m, u) in mu.items():
        assert 0.0 < m < 1.0 and 0.0 < u < 1.0, (axis, m, u)
    assert mu["street"][0] > mu["street"][1]
    weights = _fs_weights(mu, _EPS)
    assert weights["street"] > 0.5 and weights["house_number"] > 0.5
    assert weights["city"] < 0.1
    match_mean, nonmatch_mean, threshold = diagnostics["street"]
    assert abs(match_mean - 0.9) < 1e-9 and abs(nonmatch_mean - 0.2) < 1e-9
    assert threshold == 0.5


def test_optimize_never_regresses_seed() -> None:
    records = _mu_records()
    seed = _fs_weights(_mu(records)[0], _EPS)
    optimized, _ = _optimize(records, seed, _EPS)
    assert _objective(_metrics(records, optimized, _EPS)) >= _objective(
        _metrics(records, seed, _EPS)
    )


def _margin_record(target: bool, gap: float, mutations: list[str] | None = None) -> dict:
    target_id = "target" if target else None
    rows = [
        {
            "address_id": "target" if target else "candidate",
            "is_match": target,
            "axes": {"street": {"grade": "unit", "v": gap}},
        },
        {
            "address_id": "other",
            "is_match": False,
            "axes": {"street": {"grade": "unit", "v": 0.0}},
        },
    ]
    return {
        "target_id": target_id,
        "mutations": mutations or [],
        "rows": rows,
        "mu_nonmatch": rows[1:],
    }


def test_fit_margins_uses_synthetic_expectations() -> None:
    weights = {"street": 1.0}
    records = (
        [_margin_record(True, 0.7) for _ in range(50)]
        + [_margin_record(True, 0.4, ["typo"]) for _ in range(10)]
        + [_margin_record(False, 0.0) for _ in range(20)]
    )
    fit = _fit_margins(records, weights, _EPS, 1.0, 0.3)
    assert fit is not None
    margin_a, margin_b, report = fit
    assert margin_a <= 0.7 and margin_b > 0.0
    assert report["category_accuracy"] >= 0.99
    prepared = [("C", 0.0, 2)]
    assert _sim_confidence(prepared, margin_a, margin_b)[1] == 0


def test_lone_candidate_matches_serving_confidence() -> None:
    assert _leader_category(0.0, 1, 1.0, 0.5) == "B"
