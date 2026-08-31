"""composition root: build_resolution assembles the per-generation belief branches."""

from bifrost.arms.aux_index import AuxMaps
from bifrost.composition import build_resolution
from bifrost.core.types import Axis, Decomposition


def _aux() -> AuxMaps:
    return AuxMaps.from_rows(
        postcode_dim=["1050", "6900"],
        city_rows=[("koebenhavn k", "1050"), ("skjern", "6900")],
        subloc_rows=[],
    )


def test_build_resolution_assembles_the_fixed_branch_set() -> None:
    assert len(build_resolution(_aux()).branches) == 8  # the fixed address-component set


def test_build_resolution_branches_resolve_locality() -> None:
    res = build_resolution(_aux())
    d = Decomposition(text="", city="skjern")
    beliefs = [b for branch in res.branches if (b := branch(d)) is not None]
    assert any(b.axis is Axis.CITY and b.members == frozenset({"6900"}) for b in beliefs)
