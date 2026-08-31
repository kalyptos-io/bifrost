"""shared gazetteer belief branch: a fuzzy member-matcher plus the factory the city / sub_locality
branches are built from (they differ only in axis, map, weight, field).

the query token is pre-folded and the map keys are folded; an exact hit wins, else the union of all
keys within a bounded similarity - so a bare multi-district name (e.g. 'koebenhavn' with only the
suffixed 'koebenhavn k/n/...' keys) resolves to every district's postcodes, not one arbitrary pick.
stdlib difflib, no levenshtein imported from the merge - the arms stay leaf modules.
"""

import difflib
import functools
from collections.abc import Mapping

from bifrost.core.ports import BeliefBranch
from bifrost.core.types import Axis, Belief, Decomposition, Grade

_CUTOFF = 0.8  # difflib ratio floor: a lightly-noised name resolves, an unrelated token does not


def match_members(token: str, gazetteer: Mapping[str, frozenset[str]]) -> frozenset[str] | None:
    if token in gazetteer:
        return gazetteer[token]
    keys = difflib.get_close_matches(token, gazetteer, n=len(gazetteer) or 1, cutoff=_CUTOFF)
    if not keys:
        return None
    return frozenset().union(*(gazetteer[k] for k in keys))


def gazetteer_branch(
    axis: Axis, gazetteer: Mapping[str, frozenset[str]], weight: float, field: str
) -> BeliefBranch:
    @functools.lru_cache(maxsize=4096)
    def lookup(token: str) -> frozenset[str] | None:
        return match_members(token, gazetteer)

    def branch(decomposition: Decomposition) -> Belief | None:
        token = getattr(decomposition, field)
        if token is None:
            return None
        members = lookup(token)
        if members is None:
            return None
        return Belief(axis=axis, value=token, weight=weight, grade=Grade.LOCALITY, members=members)

    return branch
