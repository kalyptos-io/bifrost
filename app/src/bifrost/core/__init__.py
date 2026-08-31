"""pure domain: types, ports (the seam), the belief merge, the pipeline. no I/O, no framework."""

from .merge import merge
from .ports import AddressSource, BeliefBranch, Decompose, Normalize
from .types import (
    AddressRow,
    Axis,
    Belief,
    Candidate,
    Capability,
    Confidence,
    Decomposition,
    Grade,
    ResolvedAddress,
    Result,
    Search,
)

__all__ = [
    "merge",
    "Normalize",
    "Decompose",
    "BeliefBranch",
    "AddressSource",
    "Decomposition",
    "Belief",
    "Axis",
    "Grade",
    "Capability",
    "AddressRow",
    "Search",
    "Candidate",
    "Confidence",
    "ResolvedAddress",
    "Result",
]
