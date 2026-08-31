"""StednavneIndex: in-memory ranking + lifecycle partition. historic skrivemåder are ordinary rows
carrying their own lifecycle (no alias table), so a lifecycle set selects across them directly."""

from bifrost.arms.stednavne_index import StednavneIndex


def _idx() -> StednavneIndex:
    return StednavneIndex(
        [
            ("s1", "Furesø", "sø", "fureso", "current"),
            ("s2", "Fure So", "sø", "fure so", "retired"),  # a historic skrivemåde
        ]
    )


def test_knn_defaults_to_current_only():
    hits = _idx().knn("fureso")
    assert [h.stednavn_id for h in hits] == ["s1"]  # the retired skrivemåde is absent by default
    assert hits[0].lifecycle == "current"


def test_knn_lifecycle_selects_the_retired_row():
    hits = _idx().knn("fure so", lifecycle=("retired",))
    assert [h.stednavn_id for h in hits] == ["s2"]
    assert hits[0].lifecycle == "retired"


def test_knn_mixed_lifecycle_returns_both():
    ids = {h.stednavn_id for h in _idx().knn("fureso", lifecycle=("current", "retired"))}
    assert ids == {"s1", "s2"}
