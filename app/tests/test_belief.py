"""belief branches: city/sub_locality gazetteer-match to {postcodes}; floor/door canonicalize."""

from bifrost.arms.belief._locality import gazetteer_branch
from bifrost.arms.belief.city import build_city
from bifrost.arms.belief.floor_door import door, floor
from bifrost.core.types import Axis, Decomposition, Grade


def _d(**fields) -> Decomposition:
    return Decomposition(text="", **fields)


def _city(gazetteer):
    return gazetteer_branch(Axis.CITY, gazetteer, 0.1, "city")


def test_city_matches_gazetteer_exact() -> None:
    b = _city({"skjern": frozenset({"6900"})})(_d(city="skjern"))
    assert b is not None
    assert b.value == "skjern"
    assert b.members == frozenset({"6900"})
    assert b.grade is Grade.LOCALITY


def test_city_fuzzy_matches_a_noised_token() -> None:
    b = _city({"koebenhavn k": frozenset({"1050"})})(_d(city="koebenhaven k"))
    assert b.members == frozenset({"1050"})  # one extra char


def test_city_bare_name_unions_all_districts() -> None:
    # a bare multi-district name unions every district's postcodes, not one arbitrary pick
    branch = _city(
        {
            "koebenhavn k": frozenset({"1300"}),
            "koebenhavn n": frozenset({"2200"}),
            "koebenhavn v": frozenset({"1500"}),
        }
    )
    assert branch(_d(city="koebenhavn")).members == frozenset({"1300", "2200", "1500"})


def test_build_city_resolves_lone_locality() -> None:
    branch = build_city({"skjern": frozenset({"6900"})})
    b = branch(_d(city="skjern"))
    assert b is not None
    assert b.members == frozenset({"6900"})
    assert b.grade is Grade.LOCALITY


def test_city_absent_when_no_token() -> None:
    branch = build_city({"skjern": frozenset({"6900"})})
    assert branch(_d()) is None
    assert branch(_d(postcode="1050")) is None  # no longer derived from postcode


def test_city_unmatched_token_is_none() -> None:
    assert _city({"skjern": frozenset({"6900"})})(_d(city="zzzzzz")) is None


def test_sub_locality_matches_gazetteer() -> None:
    branch = gazetteer_branch(
        Axis.SUB_LOCALITY, {"husum": frozenset({"2700"})}, 0.1, "sub_locality"
    )
    b = branch(_d(sub_locality="husum"))
    assert b is not None
    assert b.members == frozenset({"2700"})
    assert b.grade is Grade.LOCALITY


def test_floor_canonicalizes_synonym() -> None:
    assert floor(_d(floor="stuen")).value == "st"


def test_floor_passes_through_numeric() -> None:
    assert floor(_d(floor="3")).value == "3"


def test_floor_absent() -> None:
    assert floor(_d()) is None


def test_door_canonicalizes_synonym() -> None:
    assert door(_d(door="venstre")).value == "tv"


def test_door_passes_through_unknown() -> None:
    assert door(_d(door="zz")).value == "zz"


def test_door_absent() -> None:
    assert door(_d()) is None
