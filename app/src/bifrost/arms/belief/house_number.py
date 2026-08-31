"""house_number: graded char distance. tier A, judge-only.

never sources: a lone husnr is non-selective (hundreds of thousands of '13's nationally). it can
only re-score rows a selective key (street/postcode) already pooled. HUSNR_FUZZY improves ranking;
a resolved candidate with a differing husnr still caps at confidence C (locked).
"""

from bifrost.core.types import Axis, Belief, Decomposition, Grade

from ._params import PARAMS


def house_number(decomposition: Decomposition) -> Belief | None:
    if decomposition.house_number is None:
        return None
    return Belief(
        axis=Axis.HOUSE_NUMBER,
        value=decomposition.house_number,
        weight=PARAMS.weights[Axis.HOUSE_NUMBER],
        grade=Grade.HUSNR_FUZZY,
    )
