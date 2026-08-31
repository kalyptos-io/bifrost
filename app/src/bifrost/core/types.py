"""core domain types - immutable, pure."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

TOP_K = 5  # result-set size: merge's top-k bound and select's render cap (one authority)


def rank_width(limit: int) -> int:
    """the k a resolution is ranked at. never below TOP_K: confidence is measured against the
    runner-up, so a narrow limit must not change the label the leader gets."""
    return max(limit, TOP_K)


# lifecycle vocab, one authority: sync's snapshot classifier imports it to stay in lockstep. every
# entity/designation carries one; a request carries a subset. ranked best -> worst (exact-tie and
# result-order tiebreak): current > preliminary > retired > abandoned
CURRENT = "current"
PRELIMINARY = "preliminary"
RETIRED = "retired"
ABANDONED = "abandoned"
LIFECYCLE_ORDER: tuple[str, ...] = (CURRENT, PRELIMINARY, RETIRED, ABANDONED)
LIFECYCLE_VALUES = frozenset(LIFECYCLE_ORDER)
LIFECYCLE_RANK = {v: i for i, v in enumerate(LIFECYCLE_ORDER)}
CURRENT_LIFECYCLE: tuple[str, ...] = (CURRENT,)  # default request set; projections stay here

# the fixed address-component set (= the belief branches); one authority for the wire components
COMPONENT_FIELDS = (
    "street",
    "house_number",
    "house_letter",
    "floor",
    "door",
    "postcode",
    "city",
    "sub_locality",
)
# every key a request may pin: the components above plus the area kinds geo dispatches on. bounds
# the cache/singleflight key: an unknown key is dropped by the pipeline but would still key the miss
PIN_FIELDS = frozenset(COMPONENT_FIELDS) | {"kommune", "sogn", "region"}


@dataclass(frozen=True, slots=True)
class Decomposition:
    """normalized query plus its soft-routed component spans (any may be absent)."""

    text: str
    street: str | None = None
    house_number: str | None = None
    house_letter: str | None = None
    floor: str | None = None
    door: str | None = None
    postcode: str | None = None
    city: str | None = None
    sub_locality: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreParams:
    """calibrated scoring knobs from the packaged score_params.json (fellegi-sunter artifact)."""

    eps: float  # the engine owns the canonical EPS; composition guards this matches it
    weights: Mapping[str, float]  # axis -> weight
    margin_a: float  # top-2 score gap for confidence A; injected into select
    margin_b: float  # below this gap the leader is flat/uncertain -> C


class Axis(StrEnum):
    """the address coordinate a belief speaks to."""

    STREET = "street"
    HOUSE_NUMBER = "house_number"
    HOUSE_LETTER = "house_letter"
    SUB_LOCALITY = "sub_locality"
    CITY = "city"
    POSTCODE = "postcode"
    FLOOR = "floor"
    DOOR = "door"


class Grade(StrEnum):
    """how a belief grades a row on its axis (selects the pure grade fn in merge)."""

    TRIGRAM = "trigram"  # graded street similarity (the one sorted stream), logged
    EXACT = "exact"  # 0/1 value match, logged
    HUSNR_FUZZY = "husnr_fuzzy"  # equal-length charwise distance, logged (12 != 13)
    POSTCODE_FUZZY = "postcode_fuzzy"  # digit-graded, logged
    LOCALITY = "locality"  # row.postcode in belief.members, logged
    UNIT = "unit"  # value-matched bonus outside the log; never penalizes absence (floor/door)


class Capability(StrEnum):
    """whether a belief may source candidates or only re-scores fetched rows."""

    JUDGE = "judge"  # random-access re-score only
    SOURCE = "source"  # judges and may contribute candidates (index-backed)


@dataclass(frozen=True, slots=True)
class Belief:
    """one branch's graded opinion over a single axis. never a hard filter."""

    axis: Axis
    value: str  # scalar carrier; "" allowed when members drives the grade
    weight: float
    grade: Grade
    capability: Capability = Capability.JUDGE
    members: frozenset[str] | None = None  # postcodes (LOCALITY) or the sourcing set (postcode)


@dataclass(frozen=True, slots=True)
class Search:
    """combined belief: a set of per-axis beliefs merged into one ranked top-k."""

    beliefs: tuple[Belief, ...]


@dataclass(frozen=True, slots=True)
class AddressRow:
    """a fetched address (thin fact joined to street_dim) plus its query-fixed street similarity.

    street/folded_street come from the street_dim join; the source populates street_similarity via
    pg_trgm so the core stays asyncpg-free and the scored value matches the stream's `<->` order.
    """

    address_id: str
    street_id: int
    street: str
    folded_street: str
    house_number: str
    house_letter: str | None
    floor: str | None
    door: str | None
    postcode: str
    sub_locality: str | None
    street_similarity: float
    # access + road point, etrs89/utm32 (epsg:25832); nullable - not every row is geocoded
    adgangspunkt_x: float | None = None
    adgangspunkt_y: float | None = None
    vejpunkt_x: float | None = None
    vejpunkt_y: float | None = None
    city: str | None = None  # point-in-time for retired rows, current otherwise; null = city-less
    lifecycle: str = "current"  # the presented lifecycle: designation if non-current, else entity


@dataclass(frozen=True, slots=True)
class Candidate:
    """one scored access address returned by the merge."""

    address_id: str
    street: str
    house_number: str
    postcode: str
    city: str
    house_letter: str | None = None
    floor: str | None = None
    door: str | None = None
    sub_locality: str | None = None
    score: float = 0.0
    adgangspunkt_x: float | None = None  # access + road point, etrs89/utm32 (epsg:25832)
    adgangspunkt_y: float | None = None
    vejpunkt_x: float | None = None
    vejpunkt_y: float | None = None
    lifecycle: str = "current"  # presented lifecycle, carried to Match.lifecycle


class Confidence(StrEnum):
    A = "A"  # exact, unique, large margin
    B = "B"  # minor edit (small street edit-distance, others exact)
    C = "C"  # uncertain / flat


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    candidate: Candidate
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class Result:
    query: str
    matches: tuple[ResolvedAddress, ...]


# --- geo / depth-parameterized feature path (street linestring + dagi area polygons) ---


class RoadGeom(NamedTuple):
    name: str
    sim: float
    geometry: str | None  # geojson (multi)linestring text, or None if the road is ungeocoded
    postcodes: tuple[str, ...]  # the postcodes this road's addresses touch
    lifecycle: str = "current"  # presented: alias designation lifecycle, else the road's own


class AreaGeom(NamedTuple):
    code: str | None
    name: str
    sim: float
    geometry: str | None  # geojson polygon text
    lifecycle: str = "current"


class EjendomType(StrEnum):
    """bestemt fast ejendom property type; mirrors the db CHECK + the wire type strings."""

    SAMLET_FAST_EJENDOM = "samlet_fast_ejendom"
    EJERLEJLIGHED = "ejerlejlighed"
    BYGNING_PAA_FREMMED_GRUND = "bygning_paa_fremmed_grund"


class PropertyRef(NamedTuple):
    bfe: str
    type: EjendomType


@dataclass(frozen=True, slots=True)
class EjendomInfo:
    """a property's legal nesting: parent ancestry (nearest -> ground, self excluded) + children."""

    bfe: str
    type: EjendomType
    parents: tuple[PropertyRef, ...]  # ancestry nearest -> ground, self excluded, depth <= 2
    parents_complete: bool  # false = dangling legal link in source data (expected, not corruption)
    children: tuple[PropertyRef, ...]  # direct children, uncapped
    ejerlejlighedsnummer: str | None


class EjendomGeom(NamedTuple):
    bfe: str
    type: EjendomType
    ejerlejlighedsnummer: str | None
    chain: tuple[PropertyRef, ...]  # ordered self -> ground, includes self
    chain_complete: bool
    children: tuple[PropertyRef, ...]  # direct children, uncapped
    # matched parcel (the ground sfe's, or the address's own on projection); null on truncated chain
    jordstykke: str | None
    matrikelnummer: str | None
    ejerlavskode: str | None
    ejerlavsnavn: str | None
    kommunekode: str | None
    kommunenavn: str | None
    centroid: str | None  # "x y" epsg:25832 parcel centroid
    matrikelbetegnelse: str | None  # "<matrikelnummer> <ejerlavsnavn>" display
    sim: float
    geometry: str | None  # geojson text: the ground sfe footprint (merged for multi-parcel sfes)
    lifecycle: str = "current"  # presented lifecycle (non-current parcel designation, else entity)


class StednavnGeom(NamedTuple):
    name: str
    type: str  # place-name object type (sø/skov/bebyggelse/...); returned, never filtered on
    sim: float
    geometry: str | None  # geojson point/line/polygon text
    lifecycle: str = "current"


class FeatureKind(StrEnum):
    """the depth a resolved feature speaks to; the wire `kind` discriminator."""

    ADDRESS = "address"  # husnr-depth: a point (the address engine's native output)
    STREET = "street"  # linestring
    POSTCODE = "postcode"  # postnummer polygon
    CITY = "city"  # union of a postdistrikt's postnummer polygons
    KOMMUNE = "kommune"
    SOGN = "sogn"
    REGION = "region"
    RETSKREDS = "retskreds"  # court district (dagi polygon)
    POLITIKREDS = "politikreds"  # police district (dagi polygon)
    OPSTILLINGSKREDS = "opstillingskreds"  # nomination district (dagi polygon)
    EJENDOM = "ejendom"  # bestemt fast ejendom property card (bfe); ground sfe footprint
    STEDNAVNE = "stednavne"  # named place (danske stednavne); point/line/polygon, search-only


# admin layers a resolved address projects to via its denormalized area codes (the addresses fact)
AREA_PROJECTION_TARGETS = frozenset(
    {"kommune", "sogn", "region", "retskreds", "politikreds", "opstillingskreds"}
)
# every layer a resolved address re-expresses to. ejendom projects via addresses.ejendom_bfe but
# resolves off the ejendom table, not AreaIndex; the rest via their denormalized area code.
PROJECTION_TARGETS = frozenset({"street", "postcode", "city", "ejendom"}) | AREA_PROJECTION_TARGETS
# /resolve `project` = altitude to report a resolved address at. address completes (or abstains),
# auto = the deepest feature with signal, the rest re-express the resolved address at that layer.
RESOLVE_TARGETS = frozenset({"address", "auto"}) | PROJECTION_TARGETS
# /search `target` = a standalone register matched by name/code (one SOURCE branch, no segmenter);
# no address/auto (those need the merge engine). stednavne is search-only (never a resolve target:
# not a partition, no address hierarchy to project onto).
SEARCH_TARGETS = (
    frozenset({"street", "postcode", "city", "ejendom", "stednavne"}) | AREA_PROJECTION_TARGETS
)

# wire-typed forms: /resolve renders `project`, /search renders `target`, as openapi enums; derived
# from the sets above to stay single-source, sorted for a stable spec ordering.
ProjectTarget = StrEnum("ProjectTarget", {t: t for t in sorted(RESOLVE_TARGETS)})
SearchTarget = StrEnum("SearchTarget", {t: t for t in sorted(SEARCH_TARGETS)})


@dataclass(frozen=True, slots=True)
class Feature:
    """a resolved non-address feature (street or area): display + geojson geometry at its depth."""

    kind: FeatureKind
    name: str  # display name (street or area)
    score: float
    components: Mapping[str, str]  # the resolved component(s) for the wire
    geometry: str | None = None  # geojson text (epsg:25832), fetched per hit; null if ungeocoded
    postcodes: tuple[str, ...] = ()  # a road's postcode set (street features); empty for areas
    ejendom: "EjendomInfo | None" = None  # property nesting, ejendom features only
    lifecycle: str = "current"  # presented lifecycle, carried to Match.lifecycle


@dataclass(frozen=True, slots=True)
class ResolvedFeature:
    feature: Feature
    confidence: Confidence


@dataclass(frozen=True, slots=True)
class FeatureResult:
    query: str
    matches: tuple[ResolvedFeature, ...]


@dataclass(frozen=True, slots=True)
class Resolution:
    """the unified cached payload: exactly one engine answers - address (merge) or feature (geo)."""

    address: Result | None = None
    feature: FeatureResult | None = None
