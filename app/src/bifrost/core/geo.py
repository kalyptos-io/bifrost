"""feature path: street linestring + dagi area polygons. a sibling to the address belief engine - it
never touches core.merge.

three entries: resolve_geo (the /resolve deepest-feature path, no husnr to complete),
project_address (re-express a resolved address at a coarser carried layer - the /resolve `project`
projection), and search_one (the /search path: one named register looked up by name/code).
"""

from collections.abc import Mapping

from .ports import GeoSource, Normalize
from .types import (
    AREA_PROJECTION_TARGETS,
    COMPONENT_FIELDS,
    CURRENT_LIFECYCLE,
    TOP_K,
    AreaGeom,
    Candidate,
    Confidence,
    Decomposition,
    EjendomGeom,
    EjendomInfo,
    EjendomType,
    Feature,
    FeatureKind,
    FeatureResult,
    ResolvedAddress,
    ResolvedFeature,
    Result,
    RoadGeom,
    StednavnGeom,
)

# area components finest -> coarsest; the deepest present wins. street (finer than all) is handled
# first - it carries a linestring, not a polygon. sogn (~2200) is finer than postnummer (~1100).
_AREA_DEPTH = ("sogn", "postcode", "city", "sub_locality", "kommune", "region")
# area kinds backed by a served polygon (city = union of its postnumre; sub_locality has none)
_AREA_KINDS = frozenset({"postcode", "city", "kommune", "sogn", "region"})


def merged_components(
    decomposition: Decomposition, explicit: Mapping[str, str] | None
) -> dict[str, str]:
    """components for dispatch: segmented fields plus any explicit pins (incl area-only kinds)."""
    comp = {f: v for f in COMPONENT_FIELDS if (v := getattr(decomposition, f))}
    if explicit:
        comp.update({k: v for k, v in explicit.items() if v})  # explicit wins, carries kommune/etc
    return comp


def _deepest_area(comp: Mapping[str, str]) -> str | None:
    for kind in _AREA_DEPTH:
        if comp.get(kind) and kind in _AREA_KINDS:
            return kind
    return None


def use_feature_engine(comp: Mapping[str, str], autocomplete: bool) -> bool:
    """feature path when not autocompleting - or when autocomplete has no address to complete to
    (only a kommune/sogn/region pin: no branch enumerates addresses by those)."""
    if comp.get("house_number"):
        return False  # a husnr pins a specific address
    if autocomplete and any(comp.get(f) for f in COMPONENT_FIELDS):
        return False  # an address component is present -> complete to an address
    return bool(comp.get("street")) or _deepest_area(comp) is not None


async def resolve_geo(
    comp: Mapping[str, str],
    geo_source: GeoSource,
    *,
    normalize: Normalize,
    query: str,
    limit: int = TOP_K,
    lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
) -> FeatureResult:
    street = comp.get("street")
    if street:  # street is the finest non-husnr feature; postcode (if present) confines it
        pinned = comp.get("postcode")
        hits = await geo_source.street_features(
            normalize(street),
            cap=limit,
            postcodes={pinned} if pinned else None,
            lifecycle=lifecycle,
        )
        feats = [
            Feature(
                FeatureKind.STREET,
                h.name,
                h.sim,
                {"street": h.name, **({"postcode": pinned} if pinned else {})},
                h.geometry,
                postcodes=h.postcodes,
                lifecycle=h.lifecycle,
            )
            for h in hits
        ]
        return _rank(query, feats)
    kind = _deepest_area(comp)
    if kind is None:
        return FeatureResult(query=query, matches=())
    value = comp[kind]
    if kind == "postcode":  # a code: normalize (strip/canonicalize) then exact lookup, no fuzz
        hits = await geo_source.area_by_code(kind, normalize(value), cap=limit, lifecycle=lifecycle)
    else:
        hits = await geo_source.area_by_name(kind, normalize(value), cap=limit, lifecycle=lifecycle)
    feats = [_area_feature(kind, h) for h in hits]
    return _rank(query, feats)


def _area_feature(kind: str, h: AreaGeom) -> Feature:
    return Feature(
        FeatureKind(kind),
        h.name,
        h.sim,
        _area_components(kind, h.code, h.name),
        h.geometry,
        lifecycle=h.lifecycle,
    )


def _area_components(kind: str, code: str | None, name: str) -> dict[str, str]:
    # postcode carries both its code and the postdistrikt name; the rest carry just the matched name
    if kind == "postcode" and code:
        return {"postcode": code, "city": name}
    return {kind: name}


def _ejendom_feature(h: EjendomGeom) -> Feature:
    return Feature(
        FeatureKind.EJENDOM,
        _ejendom_name(h),
        h.sim,
        _ejendom_components(h),
        h.geometry,
        ejendom=_ejendom_info(h),
        lifecycle=h.lifecycle,
    )


def _ejendom_name(h: EjendomGeom) -> str:
    # betegnelse is the ground sfe's parcel label; units/bpfg qualify it by type, sfe uses it bare
    if not h.matrikelbetegnelse:
        return h.bfe
    if h.type is EjendomType.EJERLEJLIGHED:
        nr = f" {h.ejerlejlighedsnummer}" if h.ejerlejlighedsnummer else ""
        return f"{h.matrikelbetegnelse}, ejerlejlighed{nr}"
    if h.type is EjendomType.BYGNING_PAA_FREMMED_GRUND:
        return f"{h.matrikelbetegnelse}, bygning på fremmed grund"
    return h.matrikelbetegnelse


def _ejendom_info(h: EjendomGeom) -> EjendomInfo:
    return EjendomInfo(
        h.bfe,
        h.type,
        h.chain[1:],  # strip self: parents are ancestry only
        h.chain_complete,
        h.children,
        h.ejerlejlighedsnummer,
    )


def _ejendom_components(h: EjendomGeom) -> dict[str, str]:
    comps = {"bfe": h.bfe}
    for k, v in (
        ("jordstykke", h.jordstykke),
        ("matrikelnummer", h.matrikelnummer),
        ("ejerlavskode", h.ejerlavskode),
        ("ejerlavsnavn", h.ejerlavsnavn),
        ("kommunekode", h.kommunekode),
        ("kommunenavn", h.kommunenavn),
        ("centroid", h.centroid),
        ("matrikelbetegnelse", h.matrikelbetegnelse),
    ):
        if v:
            comps[k] = v
    return comps


def _stednavn_feature(h: StednavnGeom) -> Feature:
    return Feature(
        FeatureKind.STEDNAVNE,
        h.name,
        h.sim,
        {"stednavne": h.name, "type": h.type},
        h.geometry,
        lifecycle=h.lifecycle,
    )


def _is_code(s: str) -> bool:
    # ascii digits only: a danish area/bfe code. isdigit() alone is true for unicode digits too,
    # which must fall to the fuzzy name path, not the authoritative code lookup.
    return s.isascii() and s.isdigit()


def search_cache_token(target: str, value: str, normalize: Normalize) -> str:
    """the value identity a /search lookup resolves on - the response cache keys on this so it folds
    EXACTLY as search_one dispatches. codes stay raw (normalize strips leading zeros: 0101->101,
    which would collide distinct codes); a raw digit ejendom token keys the merged bfe+ejerlavskode
    result; names/streets fold."""
    raw = value.strip()
    # codes/bfe stay raw; names (incl betegnelse) fold. street + stednavne are name-only (no code
    # path), so they always fold even for an all-digit query
    if target not in ("street", "stednavne") and _is_code(raw):
        return raw
    return normalize(value)


async def search_one(
    target: str,
    value: str,
    geo_source: GeoSource,
    *,
    limit: int,
    normalize: Normalize,
    lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
) -> FeatureResult:
    """flat single-register lookup (the /search path): one named register, by name or code. no
    segmenter, no merge - the belief engine collapsed to a single SOURCE branch."""
    if target == "street":
        hits = await geo_source.street_features(normalize(value), cap=limit, lifecycle=lifecycle)
        feats = [
            Feature(
                FeatureKind.STREET,
                h.name,
                h.sim,
                {"street": h.name},
                h.geometry,
                h.postcodes,
                lifecycle=h.lifecycle,
            )
            for h in hits
        ]
        return _rank(value, feats)
    if target == "stednavne":  # place names: fuzzy by name only, no code lookup
        hits = await geo_source.stednavne_by_name(normalize(value), cap=limit, lifecycle=lifecycle)
        return _rank(value, [_stednavn_feature(h) for h in hits])
    raw = value.strip()
    if target == "ejendom":  # digits = bfe or ejerlavskode (merged, bfe-first); before area-code
        if _is_code(raw):
            hits = await geo_source.ejendom_by_code(raw, cap=limit, lifecycle=lifecycle)
        else:
            hits = await geo_source.ejendom_by_betegnelse(
                normalize(value), cap=limit, lifecycle=lifecycle
            )
        return _rank(value, [_ejendom_feature(h) for h in hits])
    if _is_code(raw):  # raw code, not folded; normalize strips leading zeros (0101->101)
        areas = await geo_source.area_by_code(target, raw, cap=limit, lifecycle=lifecycle)
    else:
        areas = await geo_source.area_by_name(
            target, normalize(value), cap=limit, lifecycle=lifecycle
        )
    return _rank(value, [_area_feature(target, h) for h in areas])


async def project_address(
    result: Result,
    target: str,
    geo_source: GeoSource,
    *,
    normalize: Normalize,
    query: str,
    limit: int = TOP_K,
) -> FeatureResult:
    """report a resolved address at a coarser already-carried layer (the /resolve `target` path):
    collapse the candidates onto the projected value, then fetch that layer's geometry."""
    cands = [ra.candidate for ra in result.matches]
    values = await _project_values(cands, target, geo_source)
    conf = _project_confidence(result.matches, values)
    seen: dict[str, Candidate] = {}
    for v, c in zip(values, cands, strict=True):
        if v and v not in seen:  # dedup, best-first
            seen[v] = c
    feats = await _project_features(target, list(seen)[:limit], seen, geo_source, normalize)
    matches = tuple(
        ResolvedFeature(f, conf if i == 0 else Confidence.C) for i, f in enumerate(feats)
    )
    return FeatureResult(query=query, matches=matches)


async def _project_features(
    target: str,
    values: list[str],
    seen: dict[str, Candidate],
    geo_source: GeoSource,
    normalize: Normalize,
) -> list[Feature]:
    # every layer batches into one fetch; codes look up raw (authoritative), names/streets fold
    if target == "ejendom":
        # dedup key is ejendom_bfe; each winner's own parcel is the card's matched-parcel context
        ids = [seen[v].address_id for v in values]
        ctx = await geo_source.address_area_codes(ids, "jordstykke")
        pairs = [(v, ctx.get(seen[v].address_id)) for v in values]
        geoms = await geo_source.ejendom_by_bfes(pairs)
        return [
            _ejendom_feature(geoms[v])
            if v in geoms
            else Feature(FeatureKind.EJENDOM, v, 1.0, {"bfe": v})
            for v in values
        ]
    if target in AREA_PROJECTION_TARGETS:
        kind = FeatureKind(target)
        geoms = await geo_source.areas_by_codes(target, values)
        return [_area_projected(kind, target, v, geoms.get(v)) for v in values]
    if target == "postcode":
        geoms = await geo_source.areas_by_codes("postcode", values)
        return [_postcode_projected(v, seen[v], geoms.get(v)) for v in values]
    if target == "city":
        keys = {v: normalize(v) for v in values}
        geoms = await geo_source.areas_by_names("city", list(keys.values()))
        return [_city_projected(v, geoms.get(keys[v])) for v in values]
    keys = {v: normalize(v) for v in values}
    pairs = [(keys[v], {seen[v].postcode} if seen[v].postcode else None) for v in values]
    roads = await geo_source.streets_by_names(pairs)
    return [_street_projected(v, roads.get(keys[v])) for v in values]


def _area_projected(kind: FeatureKind, target: str, value: str, h: AreaGeom | None) -> Feature:
    if h is None:  # stamped code with no served polygon -> bare code, no geometry
        return Feature(kind, value, 1.0, {target: value})
    return Feature(kind, h.name, 1.0, _area_components(target, h.code, h.name), h.geometry)


async def _project_values(
    cands: list[Candidate], target: str, geo_source: GeoSource
) -> list[str | None]:
    # dedup key per candidate: a denormalized code/bfe (off the stream) or a carried field
    if target in AREA_PROJECTION_TARGETS or target == "ejendom":
        codes = await geo_source.address_area_codes([c.address_id for c in cands], target)
        return [codes.get(c.address_id) for c in cands]
    return [_project_value(c, target) for c in cands]


def _project_value(c: Candidate, target: str) -> str | None:
    return {"street": c.street, "postcode": c.postcode, "city": c.city}[target]


def _postcode_projected(value: str, cand: Candidate, h: AreaGeom | None) -> Feature:
    comps = {"postcode": value, **({"city": cand.city} if cand.city else {})}
    return Feature(FeatureKind.POSTCODE, value, 1.0, comps, h.geometry if h else None)


def _city_projected(value: str, h: AreaGeom | None) -> Feature:
    return Feature(FeatureKind.CITY, value, 1.0, {"city": value}, h.geometry if h else None)


def _street_projected(value: str, h: RoadGeom | None) -> Feature:
    geom, pcs = (h.geometry, h.postcodes) if h else (None, ())
    return Feature(FeatureKind.STREET, value, 1.0, {"street": value}, geom, postcodes=pcs)


def _project_confidence(
    matches: tuple[ResolvedAddress, ...], values: list[str | None]
) -> Confidence:
    # agreement among candidates lifts confidence (shared postcode is sure though husnr isn't); a
    # lone stamped candidate has no corroboration -> defer to its own resolution confidence
    stamped = [(m.confidence, v) for m, v in zip(matches, values, strict=True) if v]
    if not stamped:
        return Confidence.C
    if len(stamped) == 1:
        return stamped[0][0]
    top = stamped[0][1]
    agree = sum(1 for _, v in stamped if v == top)
    if agree == len(stamped):
        return Confidence.A
    return Confidence.B if agree * 2 > len(stamped) else Confidence.C


def _rank(query: str, feats: list[Feature]) -> FeatureResult:
    matches = tuple(ResolvedFeature(f, _confidence(i, feats)) for i, f in enumerate(feats))
    return FeatureResult(query=query, matches=matches)


def _confidence(i: int, feats: list[Feature]) -> Confidence:
    if i != 0:
        return Confidence.C  # only the leader can be confident
    sim = feats[0].score
    if len(feats) >= 2 and feats[1].score >= sim - 1e-9:
        return Confidence.C  # flat top -> ambiguous
    return (
        Confidence.A if sim >= 1.0 - 1e-9 else Confidence.B
    )  # exact name/code -> A, fuzzy edit -> B
