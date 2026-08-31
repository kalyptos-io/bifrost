"""render: resolved candidate -> danish betegnelse, segments joined by ', ', empties dropped."""

from bifrost.core.render import render
from bifrost.core.types import Candidate


def _c(**kw) -> Candidate:
    base = {
        "address_id": "x",
        "street": "Randersgade",
        "house_number": "48",
        "postcode": "2100",
        "city": "København Ø",
    }
    return Candidate(**{**base, **kw})


def test_full_address_orders_street_unit_locality() -> None:
    assert render(_c(house_letter="A", floor="st", door="th")) == (
        "Randersgade 48A, st. th, 2100 København Ø"
    )


def test_no_unit_drops_the_unit_segment() -> None:
    assert render(_c()) == "Randersgade 48, 2100 København Ø"


def test_floor_only_keeps_the_dot() -> None:
    assert render(_c(floor="3")) == "Randersgade 48, 3., 2100 København Ø"


def test_empty_city_omitted() -> None:
    assert render(_c(city="")) == "Randersgade 48, 2100"
