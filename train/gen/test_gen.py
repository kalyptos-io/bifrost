"""self-checks: span integrity, no substring mislabel, determinism. run: python -m gen.test_gen"""

from __future__ import annotations

import json
import random
import tempfile
from pathlib import Path

from bifrost.arms.normalize import normalize

from .address import Address, _in_bucket, stream
from .compose import (
    _FIELD_SKIP_SHAPES,
    _FIELDS,
    _PARTIAL_SHAPES,
    FIELD_SKIP_WEIGHTS,
    Segment,
    compose,
    partial_drop,
    prefix_cut,
    render,
    spans,
)
from .generate import _SWEEP, _target, _write_hard, make_record
from .junk import _given, _noise, junk_segment, junk_text
from .mutate import (
    _TIER,
    _URL_VARIANTS,
    NoiseCfg,
    _name,
    ascii_fold,
    casing,
    double_space,
    drop_comma,
    drop_space_after_comma,
    duplicate,
    junk_suffix,
    mojibake,
    mutate,
    normalize_segs,
    space_before_comma,
    tier_with,
    token_merge,
    token_split,
    trailing,
    unit_notation,
    url_encode,
)
from .names_da import GIVEN, GIVEN_TAIL
from .validate import validate_records

_TARGET_LABELS = [
    ("street_name", "street"),
    ("house_number", "house_number"),
    ("house_letter", "house_letter"),
    ("floor", "floor"),
    ("door", "door"),
    ("sub_locality", "sub_locality"),
    ("postcode", "postcode"),
    ("city", "city"),
]

_FIXTURE = [
    Address("id-1", "Vestergade", "41", "A", "2", "tv", None, "4850", "Stubbekøbing"),
    Address("id-2", "Industrivej Nord", "25", None, None, None, "Birk", "7400", "Herning"),
    Address("id-3", "Sankt Jørgens Allé", "9", "D", "st", None, None, "1615", "København V"),
    Address("id-4", "Søvej", "3", None, "kl", "mf", None, "9000", "Aalborg"),
    Address("id-5", "Trepkasgade", None, None, None, None, None, "2100", "København Ø"),
]


def _check_spans(segs) -> None:
    raw = render(segs)
    sp = spans(segs)
    # each span substring equals the segment text that produced it
    for s, seg in zip(sp, [x for x in segs if x.label], strict=True):
        assert raw[s["start"] : s["end"]] == seg.text, (raw, s, seg)
    # in-bounds, ordered, non-overlapping
    prev = 0
    for s in sp:
        assert 0 <= s["start"] < s["end"] <= len(raw)
        assert s["start"] >= prev, sp
        prev = s["end"]


def test_normalize_segs_matches_serving_normalize() -> None:
    # THE parity gate: canonical training surface must render byte-for-byte to serving normalize()
    for a in _FIXTURE:
        for tier in (1, 2, 3):
            for seed in range(200):
                rng = random.Random(seed)
                segs, _ = mutate(compose(a, rng), tier, rng)
                if seed % 2:  # exercise the truncation path too (normalize runs after prefix_cut)
                    segs = prefix_cut(segs, rng)
                out = normalize_segs(segs)
                assert render(out) == normalize(render(segs)), (tier, seed, render(segs))
                _check_spans(out)  # spans() purity survives the pass


def test_normalize_segs_strips_recipient_marker_junk() -> None:
    # marker + no digit -> dropped (no junk span); marker + digit -> kept; bare junk -> survives
    a = _FIXTURE[0]
    rng = random.Random(0)
    base = compose(a, rng)
    marked = [Segment("att. Hans Jensen", "junk"), Segment(", ", None), *base]
    out = normalize_segs(marked)
    assert "junk" not in {s["label"] for s in spans(out)}
    assert render(out) == normalize(render(marked))

    bare = [Segment("Hans Jensen Holding", "junk"), Segment(", ", None), *base]
    assert "junk" in {s["label"] for s in spans(normalize_segs(bare))}  # no marker -> survives


def test_normalize_segs_idempotent() -> None:
    for a in _FIXTURE:
        once = normalize_segs(compose(a, random.Random(0)))
        assert render(normalize_segs(once)) == render(once)


def test_normalize_segs_preserves_embedded_zero_runs() -> None:
    glued = [Segment("Kærby", "city"), Segment("0000", "postcode")]
    assert render(normalize_segs(glued)) == normalize(render(glued))
    standalone = [Segment("0000", "postcode"), Segment(" ", None), Segment("By", "city")]
    assert render(normalize_segs(standalone)) == normalize(render(standalone))


def test_unit_comma_keeps_recipient_stripping_in_sync() -> None:
    base = [
        Segment("Testvej", "street"),
        Segment(" ", None),
        Segment("42", "house_number"),
        Segment(" ", None),
        Segment("mf", "door"),
    ]
    for seed in range(20):
        formatted, _ = unit_notation(base, random.Random(seed))
        if any(segment.label is None and "," in segment.text for segment in formatted):
            surface = [*formatted, Segment(" c/o Jensen", "junk")]
            assert render(normalize_segs(surface)) == normalize(render(surface))
            return
    raise AssertionError("comma unit notation not reached")


def test_field_skip_drops_interior() -> None:
    # autocomplete jump: street anchors, a later field kept, the interior dropped
    a = _FIXTURE[0]  # full address
    rng = random.Random(0)
    seen = set()
    for _ in range(300):
        red = partial_drop(a, rng, FIELD_SKIP_WEIGHTS, _FIELD_SKIP_SHAPES)
        assert red.street_name and not red.house_number  # street kept, interior gone
        seen.add(tuple(f for f in _FIELDS if getattr(red, f)))
    assert ("street_name", "city") in seen  # the street->city shape is reachable


def test_junk_variants_reachable() -> None:
    rng = random.Random(0)
    texts = [junk_text(rng) for _ in range(4000)]
    assert any("cvr" in t for t in texts)  # numeric junk (phone/cvr/ref)
    assert any("ring" in t or "lejlighed" in t for t in texts)  # delivery/unit free-text junk


def test_unmutated_no_mislabel() -> None:
    rng = random.Random(0)
    for a in _FIXTURE:
        segs = compose(a, rng)
        _check_spans(segs)
        got = {s["label"]: render(segs)[s["start"] : s["end"]] for s in spans(segs)}
        # component fields that are copied verbatim must match the source exactly
        assert got["street"] == a.street_name
        assert got["postcode"] == a.postcode
        assert got["city"] == a.city


def test_mutated_spans_stay_valid() -> None:
    for tier in (1, 2, 3):
        rng = random.Random(tier)
        for _ in range(2000):
            a = rng.choice(_FIXTURE)
            segs = compose(a, rng)
            segs, _ = mutate(segs, tier, rng)
            _check_spans(segs)


def test_applied_iff_changed() -> None:
    for tier in (1, 2, 3):
        rng = random.Random(100 + tier)
        for _ in range(3000):
            segs = compose(rng.choice(_FIXTURE), rng)
            base = render(segs)
            mut, applied = mutate(segs, tier, rng)
            assert (applied == []) == (render(mut) == base), (base, render(mut), applied)


def test_determinism() -> None:
    a = _FIXTURE[0]
    r1 = make_record(a, 3, random.Random(7))
    r2 = make_record(a, 3, random.Random(7))
    assert r1 == r2, (r1, r2)


def test_record_preserves_request_and_segmenter_surfaces() -> None:
    rng = random.Random(4)
    for _ in range(100):
        record = make_record(_FIXTURE[0], 3, rng)
        assert normalize(record["raw"]) == record["normalized"]
        if record["raw"] != record["normalized"]:
            break
    else:
        raise AssertionError("no noisy request surface produced")


def test_no_target_variants_reachable() -> None:
    rng = random.Random(9)
    seen = set()
    for _ in range(1000):
        record = make_record(_FIXTURE[0], 1, rng, NoiseCfg(p_no_target=1.0))
        assert record["target"] is None
        seen.update(record["mutations"])
    assert {"junk_only", "invalid_postcode", "invalid_house_number"} <= seen


def test_url_encode_variants_reachable() -> None:
    # tagger collapses on any encoding variant it never saw; all must be producible
    segs = compose(_FIXTURE[0], random.Random(0))
    seen = "".join(render(url_encode(segs, random.Random(s), _URL_VARIANTS)[0]) for s in range(200))
    assert "%20" in seen and "+" in seen, seen  # standard space + form-encoding
    assert "%25" in seen, seen  # double-encoded
    assert "%c3" in seen and "%C3" in seen, seen  # lowercase + uppercase hex


def test_normalizer_inverts_recoverable_noise() -> None:
    # parity gate: normalization-recoverable corruption families must round-trip to canonical. lossy
    # families (typo, truncate, reorder, token_*, junk, abbreviate) are out of scope - can't undo
    simple = (
        casing,
        ascii_fold,
        mojibake,
        double_space,
        space_before_comma,
        drop_space_after_comma,
        drop_comma,
        trailing,
    )
    fired: dict[str, int] = {}

    def _check(name: str, mut, canon: str) -> None:
        if render(mut) != canon:  # only assert when the family visibly fired
            fired[name] = fired.get(name, 0) + 1
            assert normalize(render(mut)) == normalize(canon), (name, render(mut), canon)

    for a in _FIXTURE:
        for seed in range(80):
            segs = compose(a, random.Random(seed))  # families are non-mutating, safe to reuse
            canon = render(segs)
            for fam in simple:
                mut, ok = fam(segs, random.Random(seed))
                if ok:
                    _check(fam.__name__, mut, canon)
            enc, ok = url_encode(segs, random.Random(seed), _URL_VARIANTS)
            if ok:
                _check("url_encode", enc, canon)
    for name in [f.__name__ for f in simple] + ["url_encode"]:
        assert fired.get(name), f"{name} never fired - vacuous parity check"


def test_given_tail_reachable() -> None:
    # must draw from the full approved list, not just curated core (else memorizable)
    if not GIVEN_TAIL:
        return  # list unbundled (offline) -> curated fallback, nothing to assert
    assert len(GIVEN_TAIL) > 1000, len(GIVEN_TAIL)
    rng = random.Random(5)
    core = set(GIVEN)
    assert any(_given(rng) not in core for _ in range(200))


def test_partial_shapes_drop_components() -> None:
    # kept components get spans + non-null target; dropped components get neither
    rng = random.Random(0)
    a = _FIXTURE[0]  # full: street/house/postcode/city all present
    for shape in _PARTIAL_SHAPES:
        p = partial_drop(a, rng, {shape: 1.0})
        assert p is not None, shape
        segs = compose(p, rng)
        _check_spans(segs)
        labels = {s["label"] for s in spans(segs)}
        assert any(getattr(p, f) for f in _FIELDS), (shape, p)  # never an empty surface
        for f in _FIELDS:
            label = "street" if f == "street_name" else f
            if getattr(p, f):
                assert label in labels, (shape, f, labels)
            else:
                assert label not in labels and getattr(p, f) in (None, ""), (shape, f, labels)


def test_partials_survive_mutation() -> None:
    # noise families compose over partial structures without inventing spans
    rng = random.Random(11)
    for _ in range(2000):
        p = partial_drop(_FIXTURE[0], rng) or _FIXTURE[0]
        mut, _ = mutate(compose(p, rng), 3, rng)
        _check_spans(mut)


def test_target_matches_spans() -> None:
    # target carries a component iff it kept a span (covers partials, truncate, optional drops)
    for tier in (1, 2, 3):
        rng = random.Random(50 + tier)
        cfg = NoiseCfg(p_partial=0.3)
        for _ in range(2000):
            rec = make_record(rng.choice(_FIXTURE), tier, rng, cfg)
            labels = {s["label"] for s in rec["spans"]}
            for field_, label in _TARGET_LABELS:
                assert bool(rec["target"][field_]) == (label in labels), (field_, rec)


def test_truncate_nulls_target_tail() -> None:
    # truncate drops trailing components; target must null them, not claim the full address
    rng = random.Random(7)
    saw = False
    for _ in range(3000):
        rec = make_record(_FIXTURE[0], 2, rng, NoiseCfg())
        if "truncate" in rec["mutations"]:
            saw = True
            labels = {s["label"] for s in rec["spans"]}
            for field_, label in _TARGET_LABELS:
                assert bool(rec["target"][field_]) == (label in labels), rec
    assert saw, "truncate never fired"


def test_prefix_cut_keeps_spans_valid() -> None:
    # cut yields a true prefix with valid spans; at least one cut lands mid-token (a char dropped)
    rng = random.Random(0)
    saw_mid = False
    for _ in range(3000):
        segs = compose(rng.choice(_FIXTURE), rng)
        full = render(segs)
        cut = prefix_cut(segs, rng)
        _check_spans(cut)
        raw = render(cut)
        assert full.startswith(raw), (full, raw)
        if cut and cut[-1].label is not None:  # last kept seg is labeled -> span runs to the edge
            assert spans(cut)[-1]["end"] == len(raw)
        labeled = [x for x in cut if x.label]
        full_labeled = [x for x in segs if x.label]
        if labeled and labeled[-1].text != full_labeled[len(labeled) - 1].text:
            saw_mid = True
    assert saw_mid, "no mid-token cut observed"


def test_prefix_cut_nulls_dropped_tail() -> None:
    # prefix_cut drops trailing/leading components; target must null exactly the absent spans
    rng = random.Random(13)
    cfg = NoiseCfg(p_typed=1.0)
    saw = False
    for _ in range(3000):
        rec = make_record(rng.choice(_FIXTURE), 1, rng, cfg)
        if "prefix_cut" in rec["mutations"]:
            saw = True
            labels = {s["label"] for s in rec["spans"]}
            for field_, label in _TARGET_LABELS:
                assert bool(rec["target"][field_]) == (label in labels), rec
    assert saw, "prefix_cut never fired"


def test_prefix_cut_all_junk_ok() -> None:
    # a cut inside a leading junk prefix yields an all-junk surface with a nulled address target
    rng = random.Random(21)
    saw = False
    for _ in range(2000):
        segs = [junk_segment(rng), Segment(", ", None), *compose(_FIXTURE[0], rng)]
        cut = prefix_cut(segs, rng)
        _check_spans(cut)
        if {s["label"] for s in spans(cut)} <= {"junk"}:  # address gone, only junk survived
            saw = True
            tgt = _target(_FIXTURE[0], spans(cut))
            for f in _FIELDS:
                assert tgt[f] in (None, ""), (f, tgt)
    assert saw, "never produced an all-junk residue"


def test_junk_suffix_appends() -> None:
    # junk_suffix appends a trailing junk-labeled segment; spans stay valid and raw grows
    rng = random.Random(0)
    saw = False
    for _ in range(500):
        segs = compose(rng.choice(_FIXTURE), rng)
        out, ok = junk_suffix(segs, rng)
        if ok:
            saw = True
            _check_spans(out)
            assert out[-1].label == "junk"
            assert len(render(out)) > len(render(segs))
    assert saw


def test_junk_noise_labeled_junk() -> None:
    # noise junk (symbol-soup / mash) is reachable and tagged junk -> the reject basin
    rng = random.Random(0)
    noises = [_noise(rng) for _ in range(200)]
    assert all(noises) and any(c in "!;*" for s in noises for c in s), noises[:5]
    seen = [junk_text(rng) for _ in range(2000)]
    assert any(c in "!;*" for s in seen for c in s), "junk_text never emitted noise"
    assert junk_segment(rng).label == "junk"


def test_token_merge_glues_components() -> None:
    # merge drops a whitespace delimiter; glued spans stay valid and raw shrinks
    rng = random.Random(0)
    saw = False
    for _ in range(1000):
        segs = compose(rng.choice(_FIXTURE), rng)
        merged, ok = token_merge(segs, rng)
        if ok:
            saw = True
            _check_spans(merged)
            assert render(merged) != render(segs)
    assert saw


def test_token_split_keeps_one_span() -> None:
    # split inserts a stray space inside a component; its span still covers the whole value
    rng = random.Random(1)
    saw = False
    for _ in range(1000):
        segs = compose(rng.choice(_FIXTURE), rng)
        split, ok = token_split(segs, rng)
        if ok:
            saw = True
            _check_spans(split)
            assert render(split) != render(segs)
    assert saw


def test_url_encode_probe_targets_variants() -> None:
    # probe must hit the variant-rich url_encode (tiers 2/3), not tier-1 standard-only
    fns = [fn for fn, _ in _TIER[tier_with("url_encode")] if _name(fn) == "url_encode"]
    assert fns and getattr(fns[0], "keywords", {}).get("variants") is _URL_VARIANTS


def test_stream_skips_componentless_rows() -> None:
    # a row with no renderable component must be skipped, not crash compose
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        f.write(json.dumps({"id": "empty"}) + "\n")
        f.write(json.dumps({"id": "ok", "street_name": "Algade", "house_number": "5"}) + "\n")
        path = f.name
    try:
        rows = list(stream(path))
    finally:
        Path(path).unlink()
    assert [a.id for a in rows] == ["ok"]


def test_duplicate_glues_component() -> None:
    # duplicate repeats a postcode/city component; spans stay valid and raw grows
    rng = random.Random(0)
    saw = False
    for _ in range(1000):
        segs = compose(rng.choice(_FIXTURE), rng)
        dup, ok = duplicate(segs, rng)
        if ok:
            saw = True
            _check_spans(dup)
            assert len(render(dup)) > len(render(segs))
    assert saw


def test_generated_record_validates() -> None:
    record = make_record(
        _FIXTURE[0],
        2,
        random.Random(3),
        NoiseCfg(p_partial=1.0, p_no_target=0.0),
    )
    _, errors = validate_records([record], require_coverage=False)
    assert not errors, errors


def test_validator_rejects_bad_normalized_surface() -> None:
    record = make_record(_FIXTURE[0], 1, random.Random(0))
    record["normalized"] += "x"
    _, errors = validate_records([record], require_coverage=False)
    assert any("normalization" in error for error in errors)


def test_hard_sweep_covers_band() -> None:
    # the hard set must span the full intensity band, including the >1.0 strata
    rows = _FIXTURE * 10
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as f:
        path = f.name
    try:
        _write_hard(path, rows, random.Random(0), NoiseCfg())
        recs = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    finally:
        Path(path).unlink()
    levels = {r["intensity"] for r in recs}
    assert levels == set(_SWEEP), levels
    assert any(r["intensity"] > 1.0 for r in recs), levels  # high-intensity tail present


def test_train_held_buckets_disjoint() -> None:
    # leakage invariant: every registry id falls in exactly one of the train/held buckets
    for i in range(2000):
        id_ = f"id-{i}"
        in_train, in_held = _in_bucket(id_, "train"), _in_bucket(id_, "held")
        assert in_train != in_held, id_


def demo() -> None:
    test_unmutated_no_mislabel()
    test_mutated_spans_stay_valid()
    test_applied_iff_changed()
    test_determinism()
    test_url_encode_variants_reachable()
    test_normalizer_inverts_recoverable_noise()
    test_normalize_segs_matches_serving_normalize()
    test_normalize_segs_strips_recipient_marker_junk()
    test_normalize_segs_idempotent()
    test_field_skip_drops_interior()
    test_junk_variants_reachable()
    test_given_tail_reachable()
    test_partial_shapes_drop_components()
    test_partials_survive_mutation()
    test_target_matches_spans()
    test_truncate_nulls_target_tail()
    test_prefix_cut_keeps_spans_valid()
    test_prefix_cut_nulls_dropped_tail()
    test_prefix_cut_all_junk_ok()
    test_junk_suffix_appends()
    test_junk_noise_labeled_junk()
    test_token_merge_glues_components()
    test_token_split_keeps_one_span()
    test_url_encode_probe_targets_variants()
    test_stream_skips_componentless_rows()
    test_duplicate_glues_component()
    test_record_preserves_request_and_segmenter_surfaces()
    test_no_target_variants_reachable()
    test_generated_record_validates()
    test_validator_rejects_bad_normalized_surface()
    test_hard_sweep_covers_band()
    test_train_held_buckets_disjoint()
    print("[+] all checks passed")
    rng = random.Random(42)
    for tier in (1, 2, 3):
        a = rng.choice(_FIXTURE)
        rec = make_record(a, tier, rng)
        print(f"  tier {tier} ({rec['noise_level']}x {rec['mutations']}): {rec['raw']!r}")


if __name__ == "__main__":
    demo()
