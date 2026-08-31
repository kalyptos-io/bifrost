"""`_sims` parity after dropping the dead np.where wrapper.

denom = len(qt) + trglen - inter is |A∪B| >= 1 whenever qt is non-empty, so inter/denom is already
0 exactly where inter is 0; the removed np.where was dead. these pin the sims array bit-identical to
the pre-change formula and the derived ordering, over positions with zero overlap, ties, and a
sim-at-threshold edge.
"""

import numpy as np
from bifrost.arms._trigram import THRESHOLD, trigrams
from bifrost.arms.area_index import AreaHit, AreaIndex
from bifrost.arms.stednavne_index import StednavneIndex
from bifrost.arms.street_index import StreetIndex


def _ref_sims(idx, qt: set[str]) -> np.ndarray:
    # pre-change body, incl. the deleted np.where(inter > 0, inter / denom, 0.0)
    parts = [idx._inv[g] for g in qt if g in idx._inv]
    if not parts:
        return np.zeros(idx._n, dtype=np.float64)
    inter = np.bincount(np.concatenate(parts), minlength=idx._n).astype(np.float64)
    denom = len(qt) + idx._trglen - inter
    return np.where(inter > 0, inter / denom, 0.0)


def _assert_bit_identical(a: np.ndarray, b: np.ndarray) -> None:
    assert a.dtype == b.dtype == np.float64
    assert a.tobytes() == b.tobytes()


# folded "haha" shares exactly one trigram with "alpha" -> inter=1, denom=10, sim==THRESHOLD (0.1);
# "beta" shares none -> inter=0 (the branch the deleted np.where guarded).
_STREET_DIM = [
    (5, "Alpha", "alpha"),  # sim 1.0
    (3, "Alpha", "alpha"),  # tie with id 5 -> street_id asc
    (1, "Alphabet", "alphabet"),  # partial, 0 < sim < 1
    (7, "Haha", "haha"),  # sim exactly at threshold
    (2, "Beta", "beta"),  # zero overlap
]
_STREET_BRIDGE = [(5, "1000"), (3, "1000"), (1, "2000"), (7, "2000"), (2, "3000")]

_AREA_ROWS = [
    ("a5", "kommune", None, "Alpha", "alpha"),
    ("a3", "kommune", None, "Alpha", "alpha"),  # tie -> area_id asc (a3 before a5)
    ("a1", "kommune", None, "Alphabet", "alphabet"),
    ("a7", "kommune", None, "Haha", "haha"),  # sim at threshold
    ("b2", "kommune", None, "Beta", "beta"),  # zero overlap
    ("x9", "region", None, "Alpha", "alpha"),  # other kind, filtered out by knn
]

# stednavne rows are pre-sorted by stednavn_id (load_from sorts; __init__ assumes it), so the
# position index is the id-asc tiebreak. no kind dimension - the whole register ranks together.
_STEDNAVN_ROWS = [
    ("s1", "Alphabet", "sø", "alphabet", "current"),  # partial, 0 < sim < 1
    ("s3", "Alpha", "sø", "alpha", "current"),  # sim 1.0, tie with s5 -> id asc (s3 first)
    ("s5", "Alpha", "vej", "alpha", "current"),  # sim 1.0
    ("s7", "Haha", "bakke", "haha", "current"),  # sim exactly at threshold
    ("s8", "Beta", "sø", "beta", "current"),  # zero overlap
]

_QUERIES = ["alpha", "alphabet", "haha", "beta", "zzz"]


def test_street_sims_bit_identical():
    idx = StreetIndex(_STREET_DIM, _STREET_BRIDGE)
    for q in _QUERIES:
        qt = trigrams(q)
        _assert_bit_identical(idx._sims(qt), _ref_sims(idx, qt))


def test_area_sims_bit_identical():
    idx = AreaIndex(_AREA_ROWS)
    for q in _QUERIES:
        qt = trigrams(q)
        _assert_bit_identical(idx._sims(qt), _ref_sims(idx, qt))


def test_street_ranked_order_matches_pre_change():
    idx = StreetIndex(_STREET_DIM, _STREET_BRIDGE)
    for q in _QUERIES:
        sim = _ref_sims(idx, trigrams(q))
        cand = np.nonzero(sim >= THRESHOLD)[0]
        want = cand[np.lexsort((idx._street_id[cand], -sim[cand]))]
        got = idx._ranked_positions(q)
        if want.size == 0:
            assert got is None
        else:
            assert np.array_equal(got[0], want)


def test_area_knn_order_matches_pre_change():
    idx = AreaIndex(_AREA_ROWS)
    for q in _QUERIES:
        sim = _ref_sims(idx, trigrams(q))
        cand = np.nonzero(sim >= THRESHOLD)[0]
        hits = [(int(p), float(sim[p])) for p in cand if idx._kind[p] == "kommune"]
        hits.sort(key=lambda ps: (-ps[1], idx._area_id[ps[0]]))
        want = [idx._hit(pos, s) for pos, s in hits]
        assert idx.knn(q, kind="kommune", cap=64) == want


def test_stednavne_knn_order_and_tiebreak():
    idx = StednavneIndex(_STEDNAVN_ROWS)
    for q in _QUERIES:
        sim = _ref_sims(idx, trigrams(q))
        cand = np.nonzero(sim >= THRESHOLD)[0]
        want = cand[np.lexsort((cand, -sim[cand]))]  # sim desc, position (== id) asc
        got = idx.knn(q, cap=64)
        assert [h.stednavn_id for h in got] == [idx._id[p] for p in want]
        assert [h.sim for h in got] == [float(sim[p]) for p in want]
    lead = idx.knn("alpha", cap=64)
    assert [h.stednavn_id for h in lead[:2]] == ["s3", "s5"]  # sim tie -> id asc
    assert lead[0].type == "sø"  # the object type rides on the hit


def test_threshold_edge_is_included():
    # sim == THRESHOLD survives the >= cutoff, in both indexes
    assert AreaHit("a7", "kommune", None, "Haha", THRESHOLD) in AreaIndex(_AREA_ROWS).knn(
        "alpha", kind="kommune", cap=64
    )
    assert any(
        c.street_id == 7 and c.sim == THRESHOLD
        for c in StreetIndex(_STREET_DIM, _STREET_BRIDGE).knn("alpha", cap=64)
    )
