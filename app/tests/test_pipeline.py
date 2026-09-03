"""smoke test: the core pipeline wires arms correctly and select ranks + limits."""

from bifrost.core.pipeline import build_decomposition, resolve_decomposition
from bifrost.core.select import select
from bifrost.core.types import (
    AddressRow,
    Axis,
    Belief,
    Candidate,
    Capability,
    Confidence,
    Decomposition,
    Grade,
)


def _street_branch(d: Decomposition) -> Belief | None:
    if d.street is None:
        return None
    return Belief(
        axis=Axis.STREET,
        value=d.street,
        weight=1.0,
        grade=Grade.TRIGRAM,
        capability=Capability.SOURCE,
    )


def _absent_branch(d: Decomposition) -> Belief | None:
    return None


def _row(address_id: str, *, street_id: int, street: str, sim: float) -> AddressRow:
    return AddressRow(
        address_id=address_id,
        street_id=street_id,
        street=street,
        folded_street=street,
        house_number="1",
        house_letter=None,
        floor=None,
        door=None,
        postcode="1000",
        sub_locality=None,
        street_similarity=sim,
    )


class _FakeSource:
    def __init__(self, rows: list[AddressRow]) -> None:
        self._rows = rows

    async def street_stream(
        self, folded_q, *, cap, batch, collapse_units=False, postcodes=None, lifecycle
    ):
        yield self._rows

    async def by_postcodes(self, codes, folded_q, house_number, *, cap, lifecycle):
        return []


async def test_resolve_wires_filters_and_ranks():
    rows = [
        _row("1", street_id=1, street="a", sim=0.2),
        _row("2", street_id=2, street="b", sim=0.9),
    ]
    captured: dict[str, object] = {}

    def fake_decompose(text: str) -> Decomposition:
        captured["normalized"] = text
        return Decomposition(text=text, street="teglvaerksvej")

    decomposition = await build_decomposition("  Some Query  ", None, str.strip, fake_decompose)
    result = await resolve_decomposition(
        decomposition,
        branches=[_street_branch, _absent_branch],
        source=_FakeSource(rows),
        query="  Some Query  ",
        normalize=str.strip,
    )

    assert captured["normalized"] == "Some Query"  # normalize ran before decompose
    # absent branch drops out (no Axis.STREET-only crash) and the street row still ranks by score
    assert [m.candidate.address_id for m in result.matches] == ["2", "1"]  # ranked by score desc
    assert result.query == "  Some Query  "


def _cand(address_id: str, score: float, **fields) -> Candidate:
    base = {"street": "x", "house_number": "1", "postcode": "1000", "city": "c"}
    return Candidate(address_id=address_id, score=score, **(base | fields))


def test_confidence_dominant_exact_is_a():
    d = Decomposition(text="", house_number="1", postcode="1000")
    res = select("q", d, [_cand("1", 5.0), _cand("2", 0.0)])
    assert res.matches[0].confidence == Confidence.A  # large margin, discrete fields exact
    assert res.matches[1].confidence == Confidence.C  # only the leader can be confident


def test_confidence_clear_but_not_dominant_is_b():
    d = Decomposition(text="", house_number="1", postcode="1000")
    res = select("q", d, [_cand("1", 1.0), _cand("2", 0.4)])  # margin 0.6 in [MARGIN_B, MARGIN_A)
    assert res.matches[0].confidence == Confidence.B


def test_confidence_flat_margin_is_c():
    d = Decomposition(text="", house_number="1", postcode="1000")
    res = select("q", d, [_cand("1", 1.0), _cand("2", 0.9)])  # margin 0.1 < MARGIN_B
    assert res.matches[0].confidence == Confidence.C


def test_confidence_discrete_mismatch_blocks_a():
    d = Decomposition(text="", house_number="9", postcode="1000")  # husnr 9 vs candidate 1
    res = select("q", d, [_cand("1", 5.0), _cand("2", 0.0)])
    assert res.matches[0].confidence == Confidence.C  # leads, but a discrete hard field mismatches


def test_confidence_lone_candidate_is_b():
    d = Decomposition(text="", house_number="1", postcode="1000")
    res = select("q", d, [_cand("1", 5.0)])  # no rival to demonstrate a margin -> capped at B
    assert res.matches[0].confidence == Confidence.B


def test_confidence_lone_candidate_discrete_mismatch_is_c():
    d = Decomposition(text="", house_number="9", postcode="1000")  # husnr 9 vs candidate 1
    res = select("q", d, [_cand("1", 5.0)])
    assert res.matches[0].confidence == Confidence.C


def test_confidence_house_letter_case_insensitive():
    d = Decomposition(text="", house_number="1", house_letter="k", postcode="1000")  # folded query
    res = select("q", d, [_cand("1", 5.0, house_letter="K"), _cand("2", 0.0, house_letter="K")])
    assert res.matches[0].confidence == Confidence.A  # "k" vs "K" is not a discrete mismatch
