"""self-checks for the segmenter (run: python -m model.test_segmenter); inline fixtures only.
the train/export/segment check is a tiny onnx round-trip + eval-contract smoke."""

from __future__ import annotations

import tempfile

import numpy as np
from bifrost.arms.segmenter import (
    LABELS,
    bio_ids,
    build_tags,
    build_vocab,
    decode_spans,
    load,
    segment,
    viterbi,
)
from eval.run import spanset


def _rec(parts: list[tuple[str, str | None]]) -> dict:
    # build a record from labeled segments so offsets are exact by construction
    raw = ""
    spans = []
    for text, label in parts:
        if label is not None:
            spans.append({"start": len(raw), "end": len(raw) + len(text), "label": label})
        raw += text
    return {"raw": raw, "spans": spans}


_FIX = [
    _rec(
        [
            ("Vestergade", "street"),
            (" ", None),
            ("41", "house_number"),
            ("A", "house_letter"),
            (", ", None),
            ("4850", "postcode"),
            (" ", None),
            ("Stubbekøbing", "city"),
        ]
    ),
    _rec(
        [
            ("c/o Hans Jensen", "junk"),
            (", ", None),
            ("Algade", "street"),
            (" ", None),
            ("5", "house_number"),
        ]
    ),
    _rec([("9800", "postcode"), (" ", None), ("Hjørring", "city")]),
    _rec([("Holger%20Danskes%20Vej", "street"), (" ", None), ("12", "house_number")]),
]


def test_build_tags():
    tags = build_tags()
    assert tags[0] == "O"
    assert len(tags) == 1 + 2 * len(LABELS)
    assert tags[1] == "B-street" and tags[2] == "I-street"
    # B = odd id, I = even id
    assert all(tags[i].startswith("B-") for i in range(1, len(tags), 2))
    assert all(tags[i].startswith("I-") for i in range(2, len(tags), 2))


def test_bio_roundtrip():
    # well-formed spans -> tags -> spans is identity
    for rec in _FIX:
        tags = bio_ids(len(rec["raw"]), rec["spans"])
        assert set(decode_spans(tags)) == spanset(rec["spans"])


def test_bio_ids_paints_b_then_i():
    rec = _rec([("ab", "street")])  # B then I
    assert bio_ids(2, rec["spans"]) == [1, 2]


def test_bio_ids_clamps_truncated_span():
    # span extending past n is clamped to the window, not dropped
    assert bio_ids(2, [{"start": 0, "end": 5, "label": "street"}]) == [1, 2]


def test_decode_repair():
    # stray I (no open span) opens one; a label switch closes the previous
    b_street, i_street, b_city = 1, 2, 1 + 2 * LABELS.index("city")
    tags = [i_street, i_street, b_city, b_street]  # malformed: leading I, then switches
    spans = decode_spans(tags)
    assert spans == [("street", 0, 2), ("city", 2, 3), ("street", 3, 4)]
    # every span is well-formed
    assert all(s < e and lab in LABELS for lab, s, e in spans)


def test_build_vocab_deterministic():
    v1 = build_vocab(_FIX, 160)
    v2 = build_vocab(_FIX, 160)
    assert v1 == v2
    assert 0 not in v1.values() and 1 not in v1.values()  # PAD/UNK reserved
    assert "%" in v1 and "ø" in v1  # percent-encoding + danish chars covered


def test_decode_empty():
    assert decode_spans([]) == []
    assert decode_spans([0, 0, 0]) == []


def test_viterbi_roundtrip():
    # near one-hot emissions on a well-formed path -> viterbi recovers that exact path
    spans = [
        {"start": 0, "end": 4, "label": "street"},
        {"start": 5, "end": 7, "label": "house_number"},
    ]
    n_tags = len(build_tags())
    tags = bio_ids(7, spans)
    emis = np.full((7, n_tags), -10.0)
    for i, tg in enumerate(tags):
        emis[i, tg] = 10.0
    z1, z2 = np.zeros(n_tags), np.zeros((n_tags, n_tags))
    path = viterbi(emis, z1, z2, z1)
    assert path == tags
    assert set(decode_spans(path)) == {("street", 0, 4), ("house_number", 5, 7)}


def test_viterbi_always_bio_legal():
    # over random emissions an I-x can only follow its own B-x or I-x, never anything else
    n_tags = len(build_tags())
    z1, z2 = np.zeros(n_tags), np.zeros((n_tags, n_tags))
    rng = np.random.default_rng(1)
    for _ in range(50):
        path = viterbi(rng.standard_normal((rng.integers(1, 15), n_tags)) * 5, z1, z2, z1)
        prev = None
        for t in path:
            if t > 0 and t % 2 == 0:  # I-x: legal only after B-x (t-1) or I-x (t)
                assert prev in (t - 1, t), f"illegal I after {prev} in {path}"
            prev = t


def test_crf_logz_matches_brute_force():
    # forward algorithm vs exhaustive log-sum-exp over every tag path (the load-bearing crf math)
    import itertools

    import torch

    from .trainer import SegModel

    torch.manual_seed(0)
    n_tags, t = 5, 4
    model = SegModel(vocab_size=10, n_tags=n_tags, dim=8, layers=1, heads=2)
    with torch.no_grad():
        model.start_transitions.normal_()
        model.transitions.normal_()
        model.end_transitions.normal_()
    emis = torch.randn(1, t, n_tags)
    mask = torch.ones(1, t, dtype=torch.bool)
    start, trans, end = model._masked()
    logz = model._logz(emis, mask, start, trans, end)
    scores = []
    for path in itertools.product(range(n_tags), repeat=t):
        s = start[path[0]] + emis[0, 0, path[0]]
        for i in range(1, t):
            s = s + trans[path[i - 1], path[i]] + emis[0, i, path[i]]
        scores.append(s + end[path[-1]])
    assert torch.allclose(logz[0], torch.logsumexp(torch.stack(scores), 0), atol=1e-4)


def test_crf_nll_nonneg():
    # logZ includes the expected path, so negative log likelihood cannot be negative
    import torch

    from .trainer import SegModel

    torch.manual_seed(0)
    n_tags = 5
    model = SegModel(vocab_size=10, n_tags=n_tags, dim=8, layers=1, heads=2)
    with torch.no_grad():
        model.transitions.normal_()
    emis = torch.randn(1, 4, n_tags)
    tags = torch.tensor([[0, 1, 2, 0]])  # O, B-l0, I-l0, O: a legal path
    mask = torch.ones(1, 4, dtype=torch.bool)
    assert model.nll(emis, tags, mask).item() >= -1e-4


def test_val_f1_batched_equals_per_example():
    # batching the val forward must not change F1: pad keys are self-masked, rope is absolute
    import torch
    from bifrost.arms.segmenter import build_vocab, encode_chars
    from eval.run import _prf

    from .trainer import SegModel, _val_f1

    torch.manual_seed(0)
    vocab = build_vocab(_FIX, 64)
    examples = [(encode_chars(r["raw"], vocab), bio_ids(len(r["raw"]), r["spans"])) for r in _FIX]
    model = SegModel(len(vocab) + 2, len(build_tags()), dim=16, layers=1, heads=2)
    with torch.no_grad():
        model.transitions.normal_()

    model.eval()
    tp = pred = expected = 0
    with torch.no_grad():
        start = model.start_transitions.numpy()
        trans = model.transitions.numpy()
        end = model.end_transitions.numpy()
        for ids, tags in examples:  # reference: one example at a time, batch 1
            emis = model(torch.tensor([ids]))[0].numpy()
            p = set(decode_spans(viterbi(emis, start, trans, end)))
            g = set(decode_spans(tags))
            tp, pred, expected = tp + len(p & g), pred + len(p), expected + len(g)

    assert expected > 0
    assert _val_f1(model, examples, "cpu", batch=2) == _prf(tp, pred, expected)[2]


def test_train_export_segment():
    # tiny end-to-end smoke: train -> onnx export -> ort load -> segment() honors the eval contract
    from .trainer import train

    corpus = _FIX * 24  # enough rows for a couple of small batches
    with tempfile.TemporaryDirectory() as d:
        import json
        from pathlib import Path

        path = Path(d) / "train.jsonl"
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in corpus), encoding="utf-8"
        )
        out = Path(d) / "artifacts"
        train(
            str(path),
            str(out),
            dim=32,
            layers=1,
            heads=2,
            epochs=3,
            batch=16,
            lr=1e-3,
            max_len=48,
            val_frac=0.1,
            seed=0,
        )
        load(out)
        for rec in _FIX:
            spans = segment(rec["raw"])
            assert isinstance(spans, list)
            for item in spans:
                label, s, e = item  # exactly the (label, start, end) eval tuple
                assert label in LABELS
                assert 0 <= s < e <= len(rec["raw"])
                assert rec["raw"][s:e]  # native char offsets index the raw string

        # dynamic graph: varied lengths in one session, >max_len truncates, 1-char hits the floor
        assert all(e <= 48 for _, _, e in segment("Vestergade " * 20))  # len >> max_len=48
        assert isinstance(segment("4"), list)

        from .segmenter import quantize

        q = out.parent / "int8"
        quantize(out, q)
        load(q)
        spans = segment(_FIX[0]["raw"])  # int8 path loads, runs, decodes valid spans
        assert isinstance(spans, list)
        assert all(lab in LABELS and 0 <= s < e for lab, s, e in spans)


def demo() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(f"[-] {name}")
            fn()
    print("[+] all segmenter self-checks passed")


if __name__ == "__main__":
    demo()
