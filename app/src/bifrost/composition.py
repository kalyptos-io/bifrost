"""composition root: bind the concrete arms to the core pipeline + the geo feature path.

the address + geo sources are injected (they own connection state - one object satisfies both
protocols); every other arm is a stateless module-level callable. the core never imports any of
these - it only sees the wired callables. the registry-derived branches + merge context are built
per generation via build_resolution and pinned per request off the snapshot.
"""

from collections.abc import Mapping

from bifrost.arms import belief
from bifrost.arms.aux_index import AuxMaps
from bifrost.arms.belief._params import PARAMS
from bifrost.arms.decompose import decompose
from bifrost.arms.normalize import normalize
from bifrost.core.context import ResolutionContext
from bifrost.core.geo import (
    merged_components,
    project_address,
    resolve_geo,
    search_one,
    use_feature_engine,
)
from bifrost.core.merge import EPS
from bifrost.core.pipeline import build_decomposition, resolve_decomposition
from bifrost.core.ports import AddressSource, GeoSource
from bifrost.core.types import (
    CURRENT_LIFECYCLE,
    PROJECTION_TARGETS,
    TOP_K,
    Decomposition,
    Resolution,
    Result,
    rank_width,
)

# calibrate derives weights as swing/ln(1/eps) from this same eps; a divergence silently mis-ranks
if PARAMS.eps != EPS:
    raise ValueError(f"score_params.json eps={PARAMS.eps} diverges from engine EPS={EPS}")


def build_resolution(aux: AuxMaps) -> ResolutionContext:
    # the fixed component set, in branch order; the three aux-bound branches close over this gen
    branches = (
        belief.street,
        belief.house_number,
        belief.house_letter,
        belief.floor,
        belief.door,
        belief.build_postcode(aux.postcode_dim),
        belief.build_city(aux.city_map),
        belief.build_sub_locality(aux.subloc_map),
    )
    return ResolutionContext(branches=branches)


async def resolve_request(
    query: str | None,
    components: Mapping[str, str] | None,
    *,
    project: str,
    lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
    limit: int = TOP_K,
    source: AddressSource,
    geo_source: GeoSource,
    resolution: ResolutionContext,
) -> Resolution:
    # segment + pin once, then dispatch on project: a layer / feature geometry / belief merge
    k = rank_width(limit)  # rank wide, render narrow: the caller cuts the result, not the ranking
    decomposition = await build_decomposition(query or None, components, normalize, decompose)
    if project in PROJECTION_TARGETS:  # report the resolved address at a coarser carried layer
        result = await _merge(decomposition, source, query or "", resolution, lifecycle, k)
        feature = await project_address(  # projections stay current-only
            result, project, geo_source, normalize=normalize, query=query or "", limit=k
        )
        return Resolution(feature=feature)
    comp = merged_components(decomposition, components)
    if use_feature_engine(comp, project == "address"):  # address ~ autocomplete; auto ~ feature
        feature = await resolve_geo(
            comp,
            geo_source,
            normalize=normalize,
            query=query or "",
            limit=k,
            lifecycle=lifecycle,
        )
        return Resolution(feature=feature)
    return Resolution(
        address=await _merge(decomposition, source, query or "", resolution, lifecycle, k)
    )


async def _merge(
    decomposition: Decomposition,
    source: AddressSource,
    query: str,
    resolution: ResolutionContext,
    lifecycle: tuple[str, ...],
    limit: int,
) -> Result:
    return await resolve_decomposition(
        decomposition,
        branches=resolution.branches,
        source=source,
        query=query,
        normalize=normalize,
        lifecycle=lifecycle,
        limit=limit,
        margin_a=PARAMS.margin_a,
        margin_b=PARAMS.margin_b,
    )


async def search_request(
    query: str,
    target: str,
    *,
    geo_source: GeoSource,
    limit: int,
    lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
) -> Resolution:
    # the /search path: a single named register by name/code; no segmenter, no merge
    return Resolution(
        feature=await search_one(
            target, query, geo_source, limit=limit, normalize=normalize, lifecycle=lifecycle
        )
    )
