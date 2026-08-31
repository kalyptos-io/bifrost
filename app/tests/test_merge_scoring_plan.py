"""scoring-plan parity: the per-request plan behind score_row must stay bit-identical to the naive
per-belief weighted log-sum (no precompute), and rank rows identically."""

import math

from bifrost.core.merge import EPS, _levenshtein, score_row, threshold
from bifrost.core.types import AddressRow, Axis, Belief, Capability, Grade

_FIELD = {Axis.HOUSE_LETTER: "house_letter", Axis.FLOOR: "floor", Axis.DOOR: "door"}


def _row(
    address_id: str,
    *,
    house_number: str = "1",
    postcode: str = "1000",
    house_letter: str | None = None,
    floor: str | None = None,
    door: str | None = None,
    sim: float = 0.0,
) -> AddressRow:
    return AddressRow(
        address_id=address_id,
        street_id=0,
        street="X",
        folded_street="x",
        house_number=house_number,
        house_letter=house_letter,
        floor=floor,
        door=door,
        postcode=postcode,
        sub_locality=None,
        street_similarity=sim,
    )


def _naive(beliefs: tuple[Belief, ...], row: AddressRow) -> float:
    # reference: the weighted log-sum spelled out per grade, casefold + log recomputed every row
    total = 0.0
    for b in beliefs:
        match b.grade:
            case Grade.TRIGRAM:
                v = row.street_similarity
            case Grade.EXACT | Grade.UNIT:
                rv = getattr(row, _FIELD[b.axis])
                v = 1.0 if rv is not None and rv.casefold() == b.value.casefold() else 0.0
            case Grade.HUSNR_FUZZY:
                q, r = b.value, row.house_number
                if q == r:
                    v = 1.0
                elif len(q) != len(r):
                    v = 0.0
                else:
                    v = 1 - _levenshtein(q, r) / len(q)
            case Grade.POSTCODE_FUZZY:
                v = max(0.0, 1 - _levenshtein(row.postcode, b.value) / 4)
            case Grade.LOCALITY:
                v = 1.0 if row.postcode in (b.members or frozenset()) else 0.0
        total += b.weight * v if b.grade is Grade.UNIT else b.weight * math.log(EPS + v)
    return total


def _naive_threshold(beliefs: tuple[Belief, ...], s_d: float) -> float:
    # original-order TA bound: trigram at the frontier, every other axis at its _hi ceiling
    total = 0.0
    for b in beliefs:
        if b.grade is Grade.TRIGRAM:
            total += b.weight * math.log(EPS + s_d)
        elif b.grade is Grade.UNIT:
            total += b.weight
        else:
            total += b.weight * math.log(EPS + 1.0)
    return total


def _beliefs() -> tuple[Belief, ...]:
    return (
        Belief(Axis.STREET, "hovedgade", 1.0, Grade.TRIGRAM, capability=Capability.SOURCE),
        Belief(Axis.HOUSE_NUMBER, "12", 1.0, Grade.HUSNR_FUZZY),
        Belief(
            Axis.POSTCODE,
            "6900",
            0.1,
            Grade.POSTCODE_FUZZY,
            capability=Capability.SOURCE,
            members=frozenset({"6900"}),
        ),
        Belief(Axis.HOUSE_LETTER, "b", 0.4, Grade.EXACT),  # folded query vs uppercase db letter
        Belief(Axis.FLOOR, "2", 0.4, Grade.UNIT),
        Belief(Axis.CITY, "skjern", 0.1, Grade.LOCALITY, members=frozenset({"6900"})),
    )


def test_plan_scores_are_bit_identical_and_preserve_order():
    beliefs = _beliefs()
    rows = [
        _row("exact", house_number="12", postcode="6900", house_letter="B", floor="2", sim=0.95),
        _row("husnr_off", house_number="13", postcode="6900", house_letter="B", floor="2", sim=0.9),
        _row("wrong_pc", house_number="12", postcode="8000", house_letter="B", sim=0.4),
        _row(
            "letter_off", house_number="12", postcode="6900", house_letter="C", floor="2", sim=0.95
        ),
    ]

    for r in rows:
        assert score_row(beliefs, r) == _naive(beliefs, r)  # bit-identical, not merely close

    ranked = sorted(rows, key=lambda r: score_row(beliefs, r), reverse=True)
    assert [r.address_id for r in ranked] == ["exact", "husnr_off", "wrong_pc", "letter_off"]


def test_threshold_bound_is_bit_identical():
    beliefs = _beliefs()
    for s_d in (0.05, 0.5, 0.99):
        assert threshold(beliefs, s_d) == _naive_threshold(beliefs, s_d)  # not merely close
