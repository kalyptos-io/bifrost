"""api wire contract: request/response shapes + the core->wire mapper.

the engine speaks `core.types.Resolution` (an address or geo-feature result); this is the only place
that knows the http shape. one `Match` type spans every depth via a `kind`
discriminator and a unified geojson `Geometry`. components are returned in full;
`geometry`/`uuid`/`limit` are opt-ins applied here, after the cache read, so they never touch the
cache key - a resolution is computed and cached at `max(limit, TOP_K)`, and `limit` cuts over it.
"""

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from bifrost.core.render import render
from bifrost.core.types import (
    COMPONENT_FIELDS,
    LIFECYCLE_VALUES,
    PIN_FIELDS,
    TOP_K,
    Candidate,
    Confidence,
    EjendomInfo,
    EjendomType,
    FeatureKind,
    ProjectTarget,
    Resolution,
    ResolvedAddress,
    ResolvedFeature,
    SearchTarget,
)

# request ceilings, enforced in the schema so nothing unbounded is ever materialized
_MAX_INPUT = 512  # chars of query/input; normalize + cache-key work is linear in this
_MAX_COMPONENT = 128  # chars of a pinned component value
_MAX_BATCH = 1000  # items in a batch request
_MAX_RESULTS = _MAX_BATCH * TOP_K  # results summed over a batch, both endpoints
_MAX_RESOLVE_LIMIT = 20  # /resolve only: k drives merge's TA settle, so a huge k is a latency lever
_MAX_CHILDREN = 1000  # child refs on a property card

_Input = Annotated[str, StringConstraints(max_length=_MAX_INPUT)]


def _default_lifecycle() -> list[str]:
    return ["current"]


def _check_components(components: dict[str, str] | None) -> None:
    # closed key set: an unknown key is dropped by the pipeline but would still key the cache miss
    if not components:
        return
    if unknown := sorted(set(components) - PIN_FIELDS):
        raise ValueError(f"unknown component keys: {unknown}")
    if any(len(v) > _MAX_COMPONENT for v in components.values()):
        raise ValueError(f"component values must be at most {_MAX_COMPONENT} characters")


def _check_lifecycle(values: list[str]) -> None:
    # a request carries a unique subset of the four states; empty/dup/unknown 422s (never a filter)
    if not values:
        raise ValueError("lifecycle must be a non-empty list")
    if len(set(values)) != len(values):
        raise ValueError("lifecycle values must be unique")
    if unknown := sorted(set(values) - LIFECYCLE_VALUES):
        raise ValueError(f"unknown lifecycle values: {unknown}")


class ResolveItem(BaseModel):
    """one entry of a batch /resolve: a single query's resolution config, sans the response opts."""

    model_config = ConfigDict(extra="forbid")

    input: _Input | None = None
    components: dict[str, str] | None = None
    project: ProjectTarget = ProjectTarget("address")
    lifecycle: list[str] = Field(default_factory=_default_lifecycle)
    limit: int = Field(default=TOP_K, ge=1, le=_MAX_RESOLVE_LIMIT)

    @model_validator(mode="after")
    def _check(self) -> "ResolveItem":
        if not self.input and not self.components:
            raise ValueError("item needs input or components")
        _check_components(self.components)
        _check_lifecycle(self.lifecycle)
        return self


_ResolveBatch = Annotated[list[_Input | ResolveItem], Field(max_length=_MAX_BATCH)]


class AddressRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid"
    )  # a renamed/removed field 422s, never silently ignored

    query: _Input | _ResolveBatch | None = None  # str -> single; list -> per-item batch
    components: dict[str, str] | None = None  # single only; pre-segmented pins, skip the segmenter
    project: ProjectTarget = ProjectTarget("address")  # single only; report altitude
    lifecycle: list[str] = Field(default_factory=_default_lifecycle)  # single only; per-item batch
    geometry: bool = True
    uuid: bool = False
    limit: int = Field(default=TOP_K, ge=1, le=_MAX_RESOLVE_LIMIT)  # single only

    @model_validator(mode="after")
    def _check(self) -> "AddressRequest":
        if isinstance(self.query, list):  # batch: config is per-item, never hoisted
            if top := {"components", "project", "lifecycle", "limit"} & self.model_fields_set:
                raise ValueError(f"set {sorted(top)} per item, not top-level, for a batch query")
            if not self.query:  # [] counts as no query
                raise ValueError("provide query or components")
            total = sum(it.limit if isinstance(it, ResolveItem) else TOP_K for it in self.query)
            if total > _MAX_RESULTS:
                raise ValueError(f"summed limit {total} exceeds {_MAX_RESULTS} results")
            # a feature projection carries polygon geometry; only address points are small enough
            if self.geometry and (
                off := sorted(
                    {
                        it.project.value
                        for it in self.query
                        if isinstance(it, ResolveItem) and it.project != ProjectTarget("address")
                    }
                )
            ):
                raise ValueError(f"set geometry=false for a batch projecting onto {off}")
            return self
        if not self.query and not self.components:  # "" / None count as no query
            raise ValueError("provide query or components")
        _check_components(self.components)
        _check_lifecycle(self.lifecycle)
        return self


class SearchItem(BaseModel):
    """one entry of a batch /search: a register lookup, target required (no default register)."""

    model_config = ConfigDict(extra="forbid")

    input: _Input
    target: SearchTarget
    limit: int = Field(default=TOP_K, ge=1, le=100)
    lifecycle: list[str] = Field(default_factory=_default_lifecycle)

    @model_validator(mode="after")
    def _check(self) -> "SearchItem":
        if not self.input:
            raise ValueError("provide input")
        _check_lifecycle(self.lifecycle)
        return self


_SearchBatch = Annotated[list[SearchItem], Field(max_length=_MAX_BATCH)]


class SearchRequest(BaseModel):
    """flat register lookup: a name plus the register to search. no components, no segmenter."""

    model_config = ConfigDict(extra="forbid")

    query: _Input | _SearchBatch  # str -> single; list -> per-item batch
    target: SearchTarget | None = None  # single only; the register to search
    lifecycle: list[str] = Field(default_factory=_default_lifecycle)  # single only; per-item batch
    geometry: bool = False  # off by default: every search target is a polygon, not a point
    limit: int = Field(default=TOP_K, ge=1, le=100)  # single only

    @model_validator(mode="after")
    def _check(self) -> "SearchRequest":
        if isinstance(self.query, list):  # batch: register/limit are per-item
            if top := {"target", "limit", "lifecycle"} & self.model_fields_set:
                raise ValueError(f"set {sorted(top)} per item, not top-level, for a batch query")
            if not self.query:
                raise ValueError("provide query")
            if (total := sum(it.limit for it in self.query)) > _MAX_RESULTS:
                raise ValueError(f"summed limit {total} exceeds {_MAX_RESULTS} results")
            if self.geometry:  # every search target is a feature, so a batch never carries geometry
                raise ValueError("set geometry=false for a batch search")
            return self
        if not self.query:
            raise ValueError("provide query")
        if self.target is None:
            raise ValueError("provide target")
        _check_lifecycle(self.lifecycle)
        return self


_SRID = 25832  # etrs89/utm32; coords are easting/northing in metres, not lat/lon


class Geometry(BaseModel):
    srid: int = _SRID
    # geojson geometry: Point | LineString | Polygon | MultiPolygon. str = pre-serialized geojson
    # (feature path, straight off pg) spliced raw at render; dict = built point (address path)
    geojson: dict | str
    vejpunkt: tuple[float, float] | None = None  # address road point; geojson is the access pin


class Meta(BaseModel):
    score: float
    confidence: str  # always A, B or C
    uuid: str | None = None  # the dar id; opt-in only


class PropertyRef(BaseModel):
    bfe: str
    type: EjendomType


class ParentRelations(BaseModel):
    refs: list[PropertyRef]  # ancestry nearest -> ground, self excluded
    complete: bool  # false = a dangling legal link in the source data (expected, not corruption)


class ChildRelations(BaseModel):
    refs: list[PropertyRef]  # direct children
    complete: bool  # false = the card has more children than the response cap carries


class Relations(BaseModel):
    parents: ParentRelations
    children: ChildRelations


class Ejendom(BaseModel):
    """a property card's legal nesting: parent ancestry + children, as shape-symmetric ref
    containers. parents.complete=false marks a dangling legal link in source (not corruption)."""

    bfe: str
    type: EjendomType
    ejerlejlighedsnummer: str | None = None  # omitted (not null) for non-units
    relations: Relations


class Match(BaseModel):
    kind: str  # resolved depth: address|street|postcode|city|<dagi area>|ejendom|stednavne
    result: str
    lifecycle: str  # presented designation lifecycle: alias -> its lifecycle, else the entity's
    components: dict[str, str]
    postcodes: list[str] | None = None  # a road's postcode set; spans postcodes, so not a component
    ejendom: Ejendom | None = None  # property nesting; key omitted entirely for non-ejendom kinds
    geometry: Geometry | None = None
    meta: Meta


class AddressResult(BaseModel):
    query: str
    matches: list[Match]


class AddressError(BaseModel):
    query: str
    error: str


def _components(c: Candidate) -> dict[str, str]:
    # full resolved set; falsy skips None and the "" city fallback (absent, not empty)
    return {f: v for f in COMPONENT_FIELDS if (v := getattr(c, f))}


def _point(c: Candidate) -> dict | None:
    # the render pin: access point, falling back to the road point; half-null pair is not geocoded
    if c.adgangspunkt_x is not None and c.adgangspunkt_y is not None:
        return {"type": "Point", "coordinates": [c.adgangspunkt_x, c.adgangspunkt_y]}
    if c.vejpunkt_x is not None and c.vejpunkt_y is not None:
        return {"type": "Point", "coordinates": [c.vejpunkt_x, c.vejpunkt_y]}
    return None


def _vejpunkt(c: Candidate) -> tuple[float, float] | None:
    # road point, carried beside the pin (routing/snap-to-road); half-null pair dropped
    if c.vejpunkt_x is not None and c.vejpunkt_y is not None:
        return (c.vejpunkt_x, c.vejpunkt_y)
    return None


def _geometry(
    geojson: dict | str | None, vejpunkt: tuple[float, float] | None = None
) -> dict[str, Any] | None:
    return {"srid": _SRID, "geojson": geojson, "vejpunkt": vejpunkt} if geojson else None


def _confidence(conf: Confidence) -> str:
    return conf.value


def _address_match(ra: ResolvedAddress, *, geometry: bool, uuid: bool) -> dict[str, Any]:
    c = ra.candidate
    return {
        "kind": FeatureKind.ADDRESS.value,
        "result": render(c),
        "lifecycle": c.lifecycle,
        "components": _components(c),
        "postcodes": None,
        "geometry": _geometry(_point(c), _vejpunkt(c)) if geometry else None,
        "meta": {
            "score": c.score,
            "confidence": _confidence(ra.confidence),
            "uuid": c.address_id if uuid else None,
        },
    }


def _refs(refs: tuple) -> list[dict[str, str]]:
    return [{"bfe": r.bfe, "type": r.type.value} for r in refs]


def _ejendom_block(e: EjendomInfo) -> dict[str, Any]:
    block: dict[str, Any] = {"bfe": e.bfe, "type": e.type.value}
    if e.ejerlejlighedsnummer is not None:  # omitted, not null, for non-units
        block["ejerlejlighedsnummer"] = e.ejerlejlighedsnummer
    children = e.children[:_MAX_CHILDREN]
    block["relations"] = {
        "parents": {"refs": _refs(e.parents), "complete": e.parents_complete},
        "children": {"refs": _refs(children), "complete": len(children) == len(e.children)},
    }
    return block


def _feature_match(rf: ResolvedFeature, *, geometry: bool) -> dict[str, Any]:
    f = rf.feature
    # geometry is already-serialized geojson text; carry it raw, render splices it verbatim. the
    # ejendom key is emitted only for ejendom kinds, keeping other kinds byte-identical
    m: dict[str, Any] = {
        "kind": f.kind.value,
        "result": f.name,
        "lifecycle": f.lifecycle,
        "components": dict(f.components),
        "postcodes": list(f.postcodes) or None,
    }
    if f.ejendom is not None:
        m["ejendom"] = _ejendom_block(f.ejendom)
    m["geometry"] = _geometry(f.geometry if geometry else None)
    m["meta"] = {
        "score": f.score,
        "confidence": _confidence(rf.confidence),
        "uuid": None,
    }
    return m


DATA_UPDATED_HEADER = "X-Bifrost-Data-Updated"


def data_updated(ts: datetime) -> dict[str, str]:
    # a header, not a field: batch returns a bare array with no envelope to hang it on. stamped per
    # response off the pinned snapshot, so it never enters the cache key or the cached Resolution
    return {DATA_UPDATED_HEADER: ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}


# plain dicts, not the schema models above: render splices geojson on the dict, to_json emits it
def to_result(
    query: str, resolution: Resolution, *, geometry: bool, uuid: bool, limit: int
) -> dict[str, Any]:
    if resolution.feature is not None:
        matches = [
            _feature_match(rf, geometry=geometry) for rf in resolution.feature.matches[:limit]
        ]
    else:
        addr = resolution.address
        matches = [
            _address_match(ra, geometry=geometry, uuid=uuid)
            for ra in (addr.matches[:limit] if addr else ())
        ]
    return {"query": query, "matches": matches}
