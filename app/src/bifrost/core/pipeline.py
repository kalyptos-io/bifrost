"""the whole core flow. orchestration only - it knows no concrete arm."""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace

from .merge import merge
from .ports import AddressSource, BeliefBranch, Decompose, Normalize
from .select import MARGIN_A, MARGIN_B, select
from .types import COMPONENT_FIELDS, CURRENT_LIFECYCLE, TOP_K, Decomposition, Result, Search


async def build_decomposition(
    query: str | None,
    components: Mapping[str, str] | None,
    normalize: Normalize,
    decompose: Decompose,
) -> Decomposition:
    """segment the query off-loop (ORT releases the GIL), then pin explicit components over it."""
    if query:
        decomposition = await asyncio.to_thread(decompose, normalize(query))
    else:
        decomposition = Decomposition(text="")
    if components:
        pinned = {k: normalize(v) for k, v in components.items() if k in COMPONENT_FIELDS and v}
        if pinned:
            decomposition = replace(decomposition, **pinned)
    return decomposition


async def resolve_decomposition(
    decomposition: Decomposition,
    *,
    branches: Sequence[BeliefBranch],
    source: AddressSource,
    query: str,
    normalize: Normalize,
    lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
    limit: int = TOP_K,
    margin_a: float = MARGIN_A,
    margin_b: float = MARGIN_B,
) -> Result:
    beliefs = [
        b for branch in branches if (b := branch(decomposition)) is not None
    ]  # narrow, not cut
    candidates = await merge(Search(beliefs=tuple(beliefs)), source, k=limit, lifecycle=lifecycle)
    return select(
        query,
        decomposition,
        candidates,
        limit=limit,
        margin_a=margin_a,
        margin_b=margin_b,
        norm=normalize,
    )
