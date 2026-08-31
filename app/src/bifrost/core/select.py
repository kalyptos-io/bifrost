"""top-k + confidence over scored candidates. pure."""

from .ports import Normalize
from .types import (
    TOP_K,
    Candidate,
    Confidence,
    Decomposition,
    ResolvedAddress,
    Result,
)

# exact-gated discrete fields (street rides the margin; postcode strict here though scored fuzzy)
_DISCRETE_HARD = ("house_number", "house_letter", "postcode")
# soft-gated unit fields: a mismatch denies A (caps at B) but never forces C - recall untouched
_UNIT_SOFT = ("floor", "door")
_MARGIN_A = 1.0  # default A gap; composition injects the calibrated artifact value
_MARGIN_B = 0.3  # default B/C gap; composition injects the calibrated artifact value


def select(
    query: str,
    decomposition: Decomposition,
    candidates: list[Candidate],
    *,
    limit: int = TOP_K,
    margin_a: float = _MARGIN_A,
    margin_b: float = _MARGIN_B,
    norm: Normalize = str.casefold,
) -> Result:
    # merge is the sole ranking authority; already ordered + bounded. confidence reads the whole
    # ranking, not the cut, so narrowing the limit never relabels a match
    matches = tuple(
        ResolvedAddress(
            candidate=c,
            confidence=_confidence(i, candidates, decomposition, margin_a, margin_b, norm),
        )
        for i, c in enumerate(candidates[:limit])
    )
    return Result(query=query, matches=matches)


def _confidence(
    i: int,
    ranked: list[Candidate],
    decomposition: Decomposition,
    margin_a: float,
    margin_b: float,
    norm: Normalize,
) -> Confidence:
    if _mismatch(decomposition, ranked[i], _DISCRETE_HARD, norm):
        return Confidence.C
    if i != 0:
        return Confidence.C  # only the leader can be confident
    if len(ranked) < 2:
        return Confidence.B  # lone leader: no rival for a margin; street-aware A is #6/#7
    margin = ranked[0].score - ranked[1].score
    if margin < margin_b:
        return Confidence.C
    # exact leader -> A; a street edit or unit mismatch caps at B
    corrected = _street_corrected(decomposition, ranked[0], norm) or _mismatch(
        decomposition, ranked[0], _UNIT_SOFT, norm
    )
    if margin >= margin_a and not corrected:
        return Confidence.A
    return Confidence.B


def _street_corrected(decomposition: Decomposition, candidate: Candidate, norm: Normalize) -> bool:
    # genuine street edit, not canonicalization
    q, r = decomposition.street, candidate.street
    return q is not None and r is not None and norm(r) != q


def _mismatch(
    decomposition: Decomposition, candidate: Candidate, fields: tuple[str, ...], norm: Normalize
) -> bool:
    for field_name in fields:
        q = getattr(decomposition, field_name)
        if q is None:
            continue
        r = getattr(candidate, field_name)
        if r is None or norm(r) != q:  # q already normalized; fold r the same to compare
            return True
    return False
