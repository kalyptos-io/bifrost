"""house_letter: exact disambiguator (+letter -> 93.4% unique). tier B, judge (single char)."""

from bifrost.core.types import Axis, Belief, Decomposition, Grade

from ._params import PARAMS


def house_letter(decomposition: Decomposition) -> Belief | None:
    if decomposition.house_letter is None:
        return None
    return Belief(
        axis=Axis.HOUSE_LETTER,
        value=decomposition.house_letter,
        weight=PARAMS.weights[Axis.HOUSE_LETTER],
        grade=Grade.EXACT,
    )
