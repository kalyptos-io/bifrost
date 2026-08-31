"""sub_locality: gazetteer place-name -> {postcodes} membership belief. tier C, judge.

same shape as city, one rung finer: a folded sub-locality token fuzzy-matches the subloc map and
emits a LOCALITY belief over its postcodes; sources only on a lone-locality query, else re-scores.
built per generation over the gen-schema subloc map.
"""

from collections.abc import Mapping

from bifrost.arms.belief._locality import gazetteer_branch
from bifrost.arms.belief._params import PARAMS
from bifrost.core.ports import BeliefBranch
from bifrost.core.types import Axis


def build_sub_locality(subloc_map: Mapping[str, frozenset[str]]) -> BeliefBranch:
    return gazetteer_branch(
        Axis.SUB_LOCALITY, subloc_map, PARAMS.weights[Axis.SUB_LOCALITY], "sub_locality"
    )
