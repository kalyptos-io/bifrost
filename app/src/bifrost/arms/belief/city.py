"""city: gazetteer place-name -> {postcodes} membership belief. tier D, judge.

fuzzy-matches the folded city token against the map; the LOCALITY belief's members are that name's
postcodes (merge scores a row 1 on a postcode hit). sources only on a lone-locality query. built per
generation over the gen-schema city map, so its lru_cache never outlives its data.
"""

from collections.abc import Mapping

from bifrost.arms.belief._locality import gazetteer_branch
from bifrost.arms.belief._params import PARAMS
from bifrost.core.ports import BeliefBranch
from bifrost.core.types import Axis


def build_city(city_map: Mapping[str, frozenset[str]]) -> BeliefBranch:
    return gazetteer_branch(Axis.CITY, city_map, PARAMS.weights[Axis.CITY], "city")
