"""baked, non-registry aux data: the fellegi-sunter score params + hand-authored floor/door
synonyms. both ship in the package (read distroless-safe via importlib.resources). the
registry-derived maps now load per generation off the gen schema (see arms/aux_index.py)."""

from __future__ import annotations

import json
from importlib import resources
from types import MappingProxyType

from bifrost.core.types import Axis, ScoreParams

from .floor_door import DOOR_SYNONYMS, FLOOR_SYNONYMS

__all__ = [
    "DOOR_SYNONYMS",
    "FLOOR_SYNONYMS",
    "load_score_params",
]


def _load(name: str):
    return json.loads((resources.files("bifrost.db") / "artifacts" / name).read_text("utf-8"))


def load_score_params() -> ScoreParams:
    """the calibrated weights + eps + margins (fellegi-sunter artifact). validates every axis and
    both margins are present so a partial artifact fails clearly here, not as a KeyError deep in an
    unrelated belief branch (weights) or a silent mislabel in select (margins)."""
    d = _load("score_params.json")
    weights = d["weights"]
    missing = [a.value for a in Axis if a.value not in weights]
    if missing:
        raise ValueError(f"score_params.json missing weights for axes: {missing}")
    margins = d.get("margins") or {}
    if "a" not in margins or "b" not in margins:
        raise ValueError("score_params.json missing margins a/b")
    return ScoreParams(
        eps=d["eps"],
        weights=MappingProxyType(dict(weights)),
        margin_a=margins["a"],
        margin_b=margins["b"],
    )
