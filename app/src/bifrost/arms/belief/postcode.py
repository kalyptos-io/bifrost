"""postcode: digit-graded fuzzy score + fuzzy recovery source. tier D, source.

POSTCODE_FUZZY grades the raw span by digit edit distance. for sourcing it resolves a small
neighborhood of real postcodes - exact hit, else difflib over the postcode dimension - so a noised
span like '2l00' still recovers '2100'. that neighborhood rides in members; the merge fetches it via
by_postcodes and unions it, never suppressed. built per generation so the recovery cache never
outlives its dimension.
"""

import difflib
import functools

from bifrost.core.ports import BeliefBranch
from bifrost.core.types import Axis, Belief, Capability, Decomposition, Grade

from ._params import PARAMS

_FUZZY_N = 8  # cap the neighborhood; ORDER BY similarity keeps the relevant rows on truncation
_CUTOFF = 0.75  # one-char noise in a 4-digit code stays above this difflib ratio


def build_postcode(postcode_dim: list[str]) -> BeliefBranch:
    postcode_set = frozenset(postcode_dim)

    @functools.lru_cache
    def _neighbors(span: str) -> frozenset[str]:
        if span in postcode_set:
            return frozenset({span})
        return frozenset(difflib.get_close_matches(span, postcode_dim, n=_FUZZY_N, cutoff=_CUTOFF))

    def postcode(decomposition: Decomposition) -> Belief | None:
        if decomposition.postcode is None:
            return None
        return Belief(
            axis=Axis.POSTCODE,
            value=decomposition.postcode,
            weight=PARAMS.weights[Axis.POSTCODE],
            grade=Grade.POSTCODE_FUZZY,
            capability=Capability.SOURCE,
            members=_neighbors(decomposition.postcode),
        )

    return postcode
