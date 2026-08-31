"""floor / door: value-matched unit bonus, never penalizes absence. tier B, judge.

canonicalizes the small floor/door vocab (synonym + typo map) to the registry token; digits and
unknown tokens pass through. the merge adds the weight only on a value match (UNIT grade, outside
the log) - dk unit data is sparse, so a non-matching or absent building competes normally instead
of being buried; unit-granularity dedup then makes the unit the representative.
"""

from bifrost.core.types import Axis, Belief, Decomposition, Grade
from bifrost.db.aux import DOOR_SYNONYMS, FLOOR_SYNONYMS

from ._params import PARAMS


def floor(decomposition: Decomposition) -> Belief | None:
    if decomposition.floor is None:
        return None
    value = FLOOR_SYNONYMS.get(decomposition.floor, decomposition.floor)
    return Belief(axis=Axis.FLOOR, value=value, weight=PARAMS.weights[Axis.FLOOR], grade=Grade.UNIT)


def door(decomposition: Decomposition) -> Belief | None:
    if decomposition.door is None:
        return None
    value = DOOR_SYNONYMS.get(decomposition.door, decomposition.door)
    return Belief(axis=Axis.DOOR, value=value, weight=PARAMS.weights[Axis.DOOR], grade=Grade.UNIT)
