"""self-checks for the generated-corpus evaluation harness."""

from __future__ import annotations

import json

from .run import Resolution, _load, evaluate, score, span_f1

_FIXTURE = [
    {
        "raw": "encoded%20one",
        "normalized": "encoded one",
        "target": {"id": "id-1"},
        "spans": [{"start": 0, "end": 7, "label": "street"}],
        "tier": 1,
        "intensity": 1.0,
        "noise_level": 1,
        "mutations": ["url_encode"],
    },
    {
        "raw": "other",
        "normalized": "other",
        "target": {"id": "id-2"},
        "spans": [{"start": 0, "end": 5, "label": "city"}],
        "tier": 3,
        "intensity": 2.0,
        "noise_level": 0,
        "mutations": [],
    },
    {
        "raw": "invalid 0000",
        "normalized": "invalid 0",
        "target": None,
        "spans": [{"start": 8, "end": 9, "label": "postcode"}],
        "tier": 2,
        "intensity": 1.5,
        "noise_level": 1,
        "mutations": ["invalid_postcode"],
    },
]


def _oracle(raw: str) -> Resolution:
    answers = {"encoded%20one": "id-1", "other": "id-2"}
    return Resolution([answers[raw]], "A") if raw in answers else Resolution([], None)


def _null(raw: str) -> Resolution:
    return Resolution([], None)


def _fixed_rank(rank: int):
    def resolve(raw: str) -> Resolution:
        answer = _oracle(raw)
        if not answer.ids:
            return answer
        return Resolution([*(f"decoy-{i}" for i in range(rank - 1)), *answer.ids], "A")

    return resolve


def _always_confident(raw: str) -> Resolution:
    return Resolution(["fabricated"], "A")


def _oracle_segmenter(surface: str) -> list[tuple[str, int, int]]:
    for rec in _FIXTURE:
        if rec["normalized"] == surface:
            return [(span["label"], span["start"], span["end"]) for span in rec["spans"]]
    return []


def test_span_f1_primitive() -> None:
    expected = {("street", 0, 5), ("city", 6, 9)}
    assert span_f1(expected, expected) == (1.0, 1.0, 1.0)
    assert span_f1(set(), expected) == (0.0, 0.0, 0.0)


def test_oracle_scores_perfectly() -> None:
    report = score(_FIXTURE, _oracle, _oracle_segmenter)
    targeted = report["resolver"]["targeted"]["overall"]
    assert targeted["recall"][1] == targeted["recall"][5] == targeted["mrr"] == 1.0
    assert report["resolver"]["no_target"]["confident"] == 0
    assert report["segmenter"]["micro"]["f1"] == 1.0


def test_null_resolver_misses_without_false_returns() -> None:
    report = score(_FIXTURE, _null, None)["resolver"]
    assert report["targeted"]["overall"]["recall"][5] == 0.0
    assert report["no_target"]["returned"] == 0


def test_fixed_rank_metrics() -> None:
    metrics = score(_FIXTURE, _fixed_rank(3), None)["resolver"]["targeted"]["overall"]
    assert metrics["recall"][1] == 0.0 and metrics["recall"][5] == 1.0
    assert metrics["mrr"] == 1 / 3


def test_no_target_confident_return_is_counted() -> None:
    metrics = score(_FIXTURE, _always_confident, None)["resolver"]["no_target"]
    assert metrics["n"] == metrics["returned"] == metrics["confident"] == 1


def test_generator_dimensions_are_reported() -> None:
    targeted = score(_FIXTURE, _oracle, None)["resolver"]["targeted"]
    assert set(targeted["by_tier"]) == {1, 3}
    assert set(targeted["by_intensity"]) == {1.0, 2.0}
    assert set(targeted["by_noise_level"]) == {0, 1}
    assert set(targeted["by_mutation"]) == {"none", "url_encode"}


def test_segmenter_receives_normalized_surface() -> None:
    seen = []

    def segment(surface: str) -> list[tuple[str, int, int]]:
        seen.append(surface)
        return _oracle_segmenter(surface)

    score(_FIXTURE, None, segment)
    assert seen == [record["normalized"] for record in _FIXTURE]


def test_evaluate_reads_jsonl(tmp_path) -> None:
    path = tmp_path / "held.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in _FIXTURE), encoding="utf-8")
    assert evaluate(_oracle, None, path)["resolver"]["targeted"]["overall"]["n"] == 2


def test_load_dotted_path() -> None:
    assert _load("json:dumps") is json.dumps
    try:
        _load("json")
    except SystemExit:
        pass
    else:
        raise AssertionError("expected SystemExit on a spec without ':attr'")
