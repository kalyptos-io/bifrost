"""select confidence: the soft unit (floor/door) cap - only the A/B/C label, ranking untouched."""

from bifrost.arms.normalize import normalize
from bifrost.core.select import select
from bifrost.core.types import Candidate, Confidence, Decomposition


def _cand(score: float, **kw: str) -> Candidate:
    base = dict(address_id="x", street="vej", house_number="24", postcode="9400", city="by")
    return Candidate(**{**base, **kw}, score=score)


def _decomp(**kw: str) -> Decomposition:
    return Decomposition(text="", **kw)


def _lead(decomp: Decomposition, *cands: Candidate) -> Confidence:
    return select(normalize(""), decomp, list(cands)).matches[0].confidence


def test_no_unit_query_reaches_a() -> None:
    # large margin, no floor/door queried: unchanged behaviour
    assert _lead(_decomp(), _cand(5.0), _cand(3.0, address_id="y")) is Confidence.A


def test_matching_unit_reaches_a() -> None:
    decomp = _decomp(door="5")
    assert _lead(decomp, _cand(5.0, door="5"), _cand(3.0, address_id="y", door="5")) is Confidence.A


def test_unit_mismatch_caps_at_b_not_c() -> None:
    # query door 5, leader door 201: deny A (cap B) but never force C - recall untouched
    decomp = _decomp(door="5")
    conf = _lead(decomp, _cand(5.0, door="201"), _cand(3.0, address_id="y", door="201"))
    assert conf is Confidence.B


def test_unit_absent_on_leader_caps_at_b() -> None:
    # query supplies a door the building lacks (bare access address): not maximally confident
    decomp = _decomp(door="5")
    conf = _lead(decomp, _cand(5.0), _cand(3.0, address_id="y"))
    assert conf is Confidence.B


def _lead_street(q_street: str) -> Confidence:
    decomp = _decomp(street=q_street)
    cands = [_cand(5.0, street="Randersgade"), _cand(3.0, address_id="y", street="Randersgade")]
    return select(normalize(""), decomp, cands, norm=normalize).matches[0].confidence


def test_street_correction_caps_at_b() -> None:
    # typo'd street that still wins clearly: confident but corrected -> B, not A
    assert _lead_street("ramdersgade") is Confidence.B


def test_exact_street_reaches_a() -> None:
    # street matches modulo fold: exact leader with margin -> A
    assert _lead_street("randersgade") is Confidence.A


def test_discrete_mismatch_folds_via_norm() -> None:
    # danish house-letter Æ folds to "ae"; casefold-vs-casefold false-mismatched, norm(r) matches
    decomp = _decomp(house_letter="ae")
    cands = [_cand(5.0, house_letter="Æ"), _cand(3.0, address_id="y", house_letter="Æ")]
    conf = select(normalize(""), decomp, cands, norm=normalize).matches[0].confidence
    assert conf is Confidence.A
