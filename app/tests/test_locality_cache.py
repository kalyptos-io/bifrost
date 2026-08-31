"""cache parity: the gazetteer lookup + postcode recovery stay bit-identical to raw difflib."""

import difflib

from bifrost.arms.belief._locality import gazetteer_branch, match_members
from bifrost.arms.belief.postcode import _CUTOFF, _FUZZY_N, build_postcode
from bifrost.core.types import Axis, Decomposition

_GAZ = {
    "koebenhavn k": frozenset({"1300"}),
    "koebenhavn n": frozenset({"2200"}),
}
_DIM = ["1050", "2100", "2200", "6900"]


def _members(branch, token):
    b = branch(Decomposition(text="", city=token))
    return b.members if b is not None else None


def test_gazetteer_cache_matches_uncached() -> None:
    branch = gazetteer_branch(Axis.CITY, _GAZ, 0.1, "city")
    for token in ("koebenhavn k", "koebenhaven k", "zzzzzz"):  # exact, fuzzy, no-match
        expected = match_members(token, _GAZ)
        assert _members(branch, token) == expected
        assert _members(branch, token) == expected  # second call is a cache hit, still identical


def _recover(branch, span):
    return branch(Decomposition(text="", postcode=span)).members


def _expected(span):
    if span in set(_DIM):
        return frozenset({span})
    return frozenset(difflib.get_close_matches(span, _DIM, n=_FUZZY_N, cutoff=_CUTOFF))


def test_postcode_recovery_cache_matches_difflib() -> None:
    branch = build_postcode(_DIM)
    for span in ("2100", "2l00", "zzzz"):  # exact, fuzzy, no-match
        assert _recover(branch, span) == _expected(span)
        assert _recover(branch, span) == _expected(span)  # second call is a cache hit, identical
