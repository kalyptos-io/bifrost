"""the per-generation resolution context: the assembled belief branches, built once per generation
and pinned per request off the snapshot. a leaf - core.ports only.

named to avoid clashing with core.types.Resolution (the API result).
"""

from dataclasses import dataclass

from bifrost.core.ports import BeliefBranch


@dataclass(frozen=True, slots=True)
class ResolutionContext:
    branches: tuple[BeliefBranch, ...]
