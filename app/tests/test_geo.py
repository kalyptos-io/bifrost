"""resolve_geo dispatch: the street branch returns one feature per road + its postcode set."""

from __future__ import annotations

from _wire_cases import EJENDOM_COMPONENTS
from bifrost.core.geo import (
    _ejendom_feature,
    project_address,
    resolve_geo,
    search_cache_token,
    search_one,
    use_feature_engine,
)
from bifrost.core.types import (
    AreaGeom,
    Candidate,
    Confidence,
    EjendomGeom,
    EjendomType,
    FeatureKind,
    PropertyRef,
    ResolvedAddress,
    Result,
    RoadGeom,
    StednavnGeom,
)


def test_dispatch_area_only_pin_falls_to_feature_under_autocomplete() -> None:
    # kommune/sogn/region are not address branches: nothing to complete to -> feature even if on
    assert use_feature_engine({"kommune": "Aarhus"}, autocomplete=True)
    assert use_feature_engine({"sogn": "Domsognet"}, autocomplete=True)
    # an address component IS present -> autocomplete completes to an address, not a feature
    assert not use_feature_engine({"postcode": "8000"}, autocomplete=True)
    assert not use_feature_engine({"street": "Hovedgaden", "kommune": "Aarhus"}, autocomplete=True)
    # a husnr always pins a specific address
    assert not use_feature_engine({"street": "Hovedgaden", "house_number": "1"}, autocomplete=False)
    # not autocompleting: auto -> deepest feature regardless
    assert use_feature_engine({"kommune": "Aarhus"}, autocomplete=False)


class _FakeGeo:
    def __init__(self, roads: list[RoadGeom]) -> None:
        self._roads = roads
        self.calls: list[tuple] = []

    async def street_features(self, folded, *, cap, postcodes=None, lifecycle=("current",)):
        self.calls.append((folded, postcodes))
        return self._roads

    async def area_by_code(self, kind, code, *, cap, lifecycle=("current",)):
        return []

    async def area_by_name(self, kind, folded_name, *, cap, lifecycle=("current",)):
        return []

    async def ejendom_by_code(self, code, *, cap, lifecycle=("current",)):
        return []

    async def address_area_codes(self, address_ids, kind):
        return {}


_LINE_A = '{"type":"LineString","coordinates":[[0,0],[1,1]]}'
_LINE_B = '{"type":"LineString","coordinates":[[9,9],[8,8]]}'


def _norm(s: str) -> str:
    return s.lower()


async def test_street_feature_carries_postcodes_and_geometry() -> None:
    # no pin: one feature per road, name-only components, the road's postcode set carried alongside
    geo = _FakeGeo(
        [
            RoadGeom("Hovedgaden", 1.0, _LINE_A, ("6900",)),
            RoadGeom("Hovedgaden", 1.0, _LINE_B, ("8000", "8260")),
        ]
    )
    res = await resolve_geo({"street": "Hovedgaden"}, geo, normalize=_norm, query="hovedgaden")
    assert [m.feature.components for m in res.matches] == [
        {"street": "Hovedgaden"},
        {"street": "Hovedgaden"},
    ]
    assert [m.feature.postcodes for m in res.matches] == [("6900",), ("8000", "8260")]
    assert all(m.feature.kind is FeatureKind.STREET for m in res.matches)
    assert res.matches[0].feature.geometry.startswith('{"type":"LineString"')


async def test_street_postcode_pin_confines_and_echoes() -> None:
    geo = _FakeGeo([RoadGeom("Hovedgaden", 1.0, None, ("8000",))])
    res = await resolve_geo(
        {"street": "Hovedgaden", "postcode": "8000"}, geo, normalize=_norm, query="q"
    )
    assert geo.calls[0][1] == {"8000"}  # postcode threaded as the confinement set
    assert res.matches[0].feature.components == {"street": "Hovedgaden", "postcode": "8000"}


# ---- /search dispatch (single named register, no merge) + /resolve target projection ----


class _Geo:
    """records calls; returns canned street + area hits for search_one / project_address tests."""

    def __init__(
        self, *, roads=None, areas=None, codes=None, ejendom=None, stednavne=None, jordstykker=None
    ) -> None:
        self._roads = roads or []
        self._areas = areas or []
        self._ejendom = ejendom or []
        self._stednavne = stednavne or []
        self._codes = codes or {}  # address_id -> area code/ejendom_bfe, projection per-hit fetch
        self._jordstykker = jordstykker or {}  # address_id -> jordstykke, the parcel context fetch
        self.calls: list[tuple] = []

    async def street_features(self, folded, *, cap, postcodes=None, lifecycle=("current",)):
        self.calls.append(("street", folded, postcodes))
        return self._roads

    async def area_by_code(self, kind, code, *, cap, lifecycle=("current",)):
        self.calls.append(("code", kind, code))
        return self._areas

    async def area_by_name(self, kind, folded_name, *, cap, lifecycle=("current",)):
        self.calls.append(("name", kind, folded_name))
        return self._areas

    async def ejendom_by_code(self, code, *, cap, lifecycle=("current",)):
        self.calls.append(("ejendom", code))
        return self._ejendom

    async def ejendom_by_betegnelse(self, folded_name, *, cap, lifecycle=("current",)):
        self.calls.append(("ejendom_name", folded_name))
        return self._ejendom

    async def stednavne_by_name(self, folded_name, *, cap, lifecycle=("current",)):
        self.calls.append(("stednavne", folded_name))
        return self._stednavne

    async def areas_by_codes(self, kind, codes):
        self.calls.append(("codes", kind, tuple(codes)))
        return {c: self._areas[0] for c in codes} if self._areas else {}

    async def areas_by_names(self, kind, names):
        self.calls.append(("names", kind, tuple(names)))
        return {n: self._areas[0] for n in names} if self._areas else {}

    async def streets_by_names(self, pairs):
        self.calls.append(
            ("streets", tuple((n, tuple(sorted(p)) if p else None) for n, p in pairs))
        )
        return {n: self._roads[0] for n, _ in pairs} if self._roads else {}

    async def ejendom_by_bfes(self, refs):
        self.calls.append(("ebfes", tuple(refs)))
        return {b: self._ejendom[0] for b, _ in refs} if self._ejendom else {}

    async def address_area_codes(self, address_ids, kind):
        self.calls.append(("acodes", kind, tuple(address_ids)))
        src = self._jordstykker if kind == "jordstykke" else self._codes
        return {aid: src[aid] for aid in address_ids if aid in src}


def _addr(street, postcode, city, aid="a") -> ResolvedAddress:
    cand = Candidate(address_id=aid, street=street, house_number="1", postcode=postcode, city=city)
    return ResolvedAddress(candidate=cand, confidence=Confidence.A)


async def test_search_one_name_lookup_for_non_digit_query() -> None:
    geo = _Geo(areas=[AreaGeom("7001", "Vor Frue Sogn", 1.0, '{"type":"Polygon"}')])
    res = await search_one("sogn", "vor frue", geo, limit=5, normalize=_norm)
    assert geo.calls == [("name", "sogn", "vor frue")]  # non-digit -> fuzzy name lookup
    assert res.matches[0].feature.kind is FeatureKind.SOGN
    assert res.matches[0].feature.components == {"sogn": "Vor Frue Sogn"}
    assert res.matches[0].confidence is Confidence.A  # exact sim


async def test_search_one_code_lookup_for_digit_query() -> None:
    geo = _Geo(areas=[AreaGeom("2100", "København Ø", 1.0, '{"type":"Polygon"}')])
    res = await search_one("postcode", "2100", geo, limit=5, normalize=_norm)
    assert geo.calls == [("code", "postcode", "2100")]  # digits -> authoritative code lookup
    assert res.matches[0].feature.components == {"postcode": "2100", "city": "København Ø"}


async def test_search_one_street_does_not_confine() -> None:
    geo = _Geo(roads=[RoadGeom("Randersgade", 1.0, _LINE_A, ("2100",))])
    res = await search_one("street", "randersgade", geo, limit=5, normalize=_norm)
    assert geo.calls == [("street", "randersgade", None)]  # /search has no postcode pin
    assert res.matches[0].feature.postcodes == ("2100",)


async def test_search_one_city_uses_area_name() -> None:
    geo = _Geo(areas=[AreaGeom(None, "Aarhus C", 1.0, '{"type":"MultiPolygon"}')])
    res = await search_one("city", "aarhus c", geo, limit=5, normalize=_norm)
    assert geo.calls == [("name", "city", "aarhus c")]  # city is name-only (no code)
    assert res.matches[0].feature.kind is FeatureKind.CITY
    assert res.matches[0].feature.components == {"city": "Aarhus C"}


async def test_search_one_stednavne_name_only() -> None:
    geo = _Geo(stednavne=[StednavnGeom("Furesø", "sø", 1.0, '{"type":"Polygon"}')])
    res = await search_one("stednavne", "furesø", geo, limit=5, normalize=_norm)
    assert geo.calls == [("stednavne", "furesø")]  # fuzzy name lookup, no code path
    m = res.matches[0]
    assert m.feature.kind is FeatureKind.STEDNAVNE
    assert m.feature.name == "Furesø"
    assert m.feature.components == {"stednavne": "Furesø", "type": "sø"}  # type carried to the wire
    assert m.confidence is Confidence.A  # exact sim


async def test_search_one_stednavne_digits_still_take_name_branch() -> None:
    # place names never take a code branch; an all-digit query still folds + name-searches
    geo = _Geo(stednavne=[StednavnGeom("112 Bakke", "landskabsform", 0.6, None)])
    await search_one("stednavne", "112", geo, limit=5, normalize=_norm)
    assert geo.calls == [("stednavne", "112")]  # normalize(value), not a ("code", ...) lookup


def test_search_cache_token_stednavne_always_folds() -> None:
    # stednavne is name-only: an all-digit query folds (no raw code to key), unlike an area code
    strip0 = lambda s: s.lstrip("0")  # noqa: E731 - normalize-shaped fold for the test
    assert search_cache_token("stednavne", "0112", strip0) == "112"  # folded, not raw like a code
    assert search_cache_token("stednavne", "Furesø", str.lower) == "furesø"


async def test_project_postcode_unanimous_is_a() -> None:
    geo = _Geo(areas=[AreaGeom("2100", "København Ø", 1.0, '{"type":"Polygon"}')])
    result = Result(
        query="q",
        matches=(
            _addr("Randersgade", "2100", "København Ø", "a"),
            _addr("Randersgade", "2100", "København Ø", "b"),
        ),
    )
    res = await project_address(result, "postcode", geo, normalize=_norm, query="randersgade")
    assert len(res.matches) == 1  # collapsed onto the one shared postcode
    m = res.matches[0]
    assert m.feature.kind is FeatureKind.POSTCODE
    assert m.feature.components == {"postcode": "2100", "city": "København Ø"}
    assert m.feature.geometry == '{"type":"Polygon"}'  # fetched via areas_by_codes (raw code)
    assert m.confidence is Confidence.A  # candidates agree -> the postcode is certain


async def test_project_postcode_split_degrades() -> None:
    geo = _Geo(areas=[AreaGeom("2100", "København Ø", 1.0, '{"type":"Polygon"}')])
    result = Result(
        query="q",
        matches=(
            _addr("Strandvejen", "2100", "x", "a"),
            _addr("Strandvejen", "2900", "y", "b"),
        ),
    )
    res = await project_address(result, "postcode", geo, normalize=_norm, query="strandvejen")
    assert [m.feature.name for m in res.matches] == ["2100", "2900"]  # both, leader first
    assert res.matches[0].confidence is Confidence.C  # 1 of 2 agree -> not confident


async def test_project_street_confines_by_candidate_postcode() -> None:
    geo = _Geo(roads=[RoadGeom("Randersgade", 1.0, _LINE_A, ("2100",))])
    result = Result(query="q", matches=(_addr("Randersgade", "2100", "København Ø", "a"),))
    res = await project_address(result, "street", geo, normalize=_norm, query="randersgade")
    assert res.matches[0].feature.kind is FeatureKind.STREET
    assert ("streets", (("randersgade", ("2100",)),)) in geo.calls  # confined by candidate postcode
    assert res.matches[0].feature.postcodes == ("2100",)  # road's postcode set carried to the wire


async def test_project_city_carries_union_polygon() -> None:
    # city projects to its seed-built postnummer union, fetched by folded name via area_by_name
    geo = _Geo(areas=[AreaGeom(None, "København Ø", 1.0, '{"type":"MultiPolygon"}')])
    result = Result(query="q", matches=(_addr("Randersgade", "2100", "København Ø", "a"),))
    res = await project_address(result, "city", geo, normalize=_norm, query="x")
    assert res.matches[0].feature.kind is FeatureKind.CITY
    assert res.matches[0].feature.components == {"city": "København Ø"}
    assert res.matches[0].feature.geometry == '{"type":"MultiPolygon"}'
    assert ("names", "city", ("københavn ø",)) in geo.calls


class _MapGeo:
    """returns a distinct geometry per key so batched association + ordering can be asserted."""

    async def areas_by_codes(self, kind, codes):
        return {c: AreaGeom(c, f"name-{c}", 1.0, f'{{"code":"{c}"}}') for c in codes}

    async def areas_by_names(self, kind, names):
        return {n: AreaGeom(None, n, 1.0, f'{{"name":"{n}"}}') for n in names}

    async def streets_by_names(self, pairs):
        return {
            n: RoadGeom(n, 1.0, f'{{"street":"{n}"}}', tuple(sorted(p)) if p else ())
            for n, p in pairs
        }


async def test_project_postcode_batches_and_maps_each_value_in_order() -> None:
    result = Result(
        query="q",
        matches=(_addr("Alpha", "2100", "CityA", "a"), _addr("Beta", "2200", "CityB", "b")),
    )
    res = await project_address(result, "postcode", _MapGeo(), normalize=_norm, query="q")
    assert [m.feature.name for m in res.matches] == ["2100", "2200"]  # order preserved
    assert [m.feature.geometry for m in res.matches] == ['{"code":"2100"}', '{"code":"2200"}']
    # each postcode carries its own candidate's city, not a shared one
    assert [m.feature.components["city"] for m in res.matches] == ["CityA", "CityB"]


async def test_project_city_batches_and_maps_each_value_in_order() -> None:
    result = Result(
        query="q",
        matches=(_addr("Alpha", "2100", "Aarhus C", "a"), _addr("Beta", "2200", "Odense C", "b")),
    )
    res = await project_address(result, "city", _MapGeo(), normalize=_norm, query="q")
    assert [m.feature.name for m in res.matches] == ["Aarhus C", "Odense C"]
    geoms = [m.feature.geometry for m in res.matches]
    assert geoms == ['{"name":"aarhus c"}', '{"name":"odense c"}']


async def test_project_street_batches_and_maps_each_value_with_own_pin() -> None:
    result = Result(
        query="q",
        matches=(_addr("Alpha", "1000", "x", "a"), _addr("Beta", "2000", "y", "b")),
    )
    res = await project_address(result, "street", _MapGeo(), normalize=_norm, query="q")
    assert [m.feature.name for m in res.matches] == ["Alpha", "Beta"]  # order preserved
    assert [m.feature.geometry for m in res.matches] == ['{"street":"alpha"}', '{"street":"beta"}']
    assert [m.feature.postcodes for m in res.matches] == [("1000",), ("2000",)]  # each own pin


async def test_project_empty_result_yields_no_matches() -> None:
    res = await project_address(
        Result(query="q", matches=()), "postcode", _Geo(), normalize=_norm, query="x"
    )
    assert res.matches == ()


async def test_project_area_uses_stamped_code_and_gazetteer_name() -> None:
    # the address carries no kommune on the Candidate: it is fetched per-hit by address_id, then the
    # gazetteer resolves the code to a display name + polygon (not the code).
    geo = _Geo(
        areas=[AreaGeom("0751", "Aarhus", 1.0, '{"type":"Polygon"}')],
        codes={"a": "0751", "b": "0751"},
    )
    result = Result(
        query="q",
        matches=(
            _addr("Hovedgaden", "8000", "Aarhus C", "a"),
            _addr("Hovedgaden", "8000", "Aarhus C", "b"),
        ),
    )
    res = await project_address(result, "kommune", geo, normalize=_norm, query="hovedgaden 1")
    assert ("acodes", "kommune", ("a", "b")) in geo.calls  # per-hit code fetch by address_id
    assert ("codes", "kommune", ("0751",)) in geo.calls  # batched gazetteer fetch by stamped code
    assert len(res.matches) == 1  # both candidates share the kommune -> collapsed
    m = res.matches[0]
    assert m.feature.kind is FeatureKind.KOMMUNE
    assert m.feature.name == "Aarhus" and m.feature.components == {"kommune": "Aarhus"}
    assert m.feature.geometry == '{"type":"Polygon"}'
    assert m.confidence is Confidence.A  # candidates agree on the kommune


async def test_project_area_unstamped_abstains() -> None:
    # no denormalized code for the resolved address (null column) -> nothing to project -> abstain
    geo = _Geo(areas=[AreaGeom("0751", "Aarhus", 1.0, '{"type":"Polygon"}')], codes={})
    result = Result(query="q", matches=(_addr("Hovedgaden", "8000", "Aarhus C", "a"),))
    res = await project_address(result, "sogn", geo, normalize=_norm, query="x")
    assert res.matches == ()


def _eg(
    *,
    bfe="100412345",
    type=EjendomType.EJERLEJLIGHED,
    ejerlejlighedsnummer="4",
    betegnelse="1a Byrum By",
    jordstykke="1000123",
    chain=None,
    chain_complete=True,
    children=(),
    sim=1.0,
    geometry='{"type":"MultiPolygon"}',
) -> EjendomGeom:
    if chain is None:
        chain = (
            PropertyRef(bfe, type),
            PropertyRef("100400001", EjendomType.SAMLET_FAST_EJENDOM),
        )
    return EjendomGeom(
        bfe,
        type,
        ejerlejlighedsnummer,
        chain,
        chain_complete,
        children,
        jordstykke,
        "1a",
        "60851",
        "Byrum By",
        "0400",
        "Bornholm",
        "882000 6100000",
        betegnelse,
        sim,
        geometry,
    )


async def test_search_one_ejendom_by_bfe_not_area_index() -> None:
    # bfe is digits, but ejendom takes its own branch (ejendom table), not area_by_code
    geo = _Geo(ejendom=[_eg()])
    res = await search_one("ejendom", "100412345", geo, limit=5, normalize=_norm)
    assert geo.calls == [("ejendom", "100412345")]  # not a ("code", ...) area lookup
    m = res.matches[0]
    assert m.feature.kind is FeatureKind.EJENDOM
    assert m.feature.name == "1a Byrum By, ejerlejlighed 4"  # type-aware display
    assert m.feature.components == EJENDOM_COMPONENTS
    assert m.confidence is Confidence.A  # exact bfe


async def test_search_one_ejendom_by_betegnelse_when_not_digits() -> None:
    # a non-digit query is a betegnelse, not a bfe -> word-similarity name search, not by_code
    geo = _Geo(
        ejendom=[
            _eg(
                type=EjendomType.SAMLET_FAST_EJENDOM,
                ejerlejlighedsnummer=None,
                betegnelse="162 Vestervold Kvarter",
                sim=0.8,
            )
        ]
    )
    res = await search_one("ejendom", "162 Vestervold", geo, limit=5, normalize=_norm)
    assert geo.calls == [("ejendom_name", "162 vestervold")]
    m = res.matches[0]
    assert m.feature.kind is FeatureKind.EJENDOM
    assert m.feature.name == "162 Vestervold Kvarter"  # sfe -> bare betegnelse


def test_ejendom_display_names_are_type_aware() -> None:
    sfe = _ejendom_feature(_eg(type=EjendomType.SAMLET_FAST_EJENDOM, ejerlejlighedsnummer=None))
    unit = _ejendom_feature(_eg(type=EjendomType.EJERLEJLIGHED, ejerlejlighedsnummer="4"))
    bpfg = _ejendom_feature(
        _eg(type=EjendomType.BYGNING_PAA_FREMMED_GRUND, ejerlejlighedsnummer=None)
    )
    bare = _ejendom_feature(_eg(type=EjendomType.EJERLEJLIGHED, betegnelse=None))
    assert sfe.name == "1a Byrum By"
    assert unit.name == "1a Byrum By, ejerlejlighed 4"
    assert bpfg.name == "1a Byrum By, bygning på fremmed grund"
    assert bare.name == "100412345"  # no betegnelse (truncated chain) -> bare bfe


async def test_ejendom_parents_and_children_ride_the_feature() -> None:
    chain = (
        PropertyRef("100412345", EjendomType.EJERLEJLIGHED),
        PropertyRef("100400500", EjendomType.BYGNING_PAA_FREMMED_GRUND),
        PropertyRef("100400001", EjendomType.SAMLET_FAST_EJENDOM),
    )
    children = (
        PropertyRef("100400002", EjendomType.EJERLEJLIGHED),
        PropertyRef("100400003", EjendomType.EJERLEJLIGHED),
    )
    geo = _Geo(ejendom=[_eg(chain=chain, chain_complete=True, children=children)])
    res = await search_one("ejendom", "100412345", geo, limit=5, normalize=_norm)
    e = res.matches[0].feature.ejendom
    assert e.parents == chain[1:]  # nearest -> ground, self excluded
    assert e.parents_complete is True
    assert e.children == children
    assert e.type is EjendomType.EJERLEJLIGHED and e.ejerlejlighedsnummer == "4"


async def test_project_ejendom_dedups_on_bfe_and_passes_parcel_context() -> None:
    # both candidates project to the same property; the winner (first) contributes its own parcel
    geo = _Geo(
        ejendom=[_eg()],
        codes={"a": "100412345", "b": "100412345"},
        jordstykker={"a": "1000123", "b": "1000999"},
    )
    result = Result(
        query="q",
        matches=(_addr("Hovedgaden", "8000", "x", "a"), _addr("Hovedgaden", "8000", "x", "b")),
    )
    res = await project_address(result, "ejendom", geo, normalize=_norm, query="hovedgaden 1")
    assert ("acodes", "ejendom", ("a", "b")) in geo.calls  # dedup key fetched by address_id
    assert ("acodes", "jordstykke", ("a",)) in geo.calls  # only the winner's parcel fetched
    assert ("ebfes", (("100412345", "1000123"),)) in geo.calls  # winner's parcel as context
    assert len(res.matches) == 1  # both share the property -> collapsed
    m = res.matches[0]
    assert m.feature.kind is FeatureKind.EJENDOM
    assert m.feature.components == EJENDOM_COMPONENTS
    assert m.feature.geometry == '{"type":"MultiPolygon"}'
    assert m.confidence is Confidence.A  # candidates agree on the property


async def test_project_ejendom_bare_fallback_when_card_missing() -> None:
    # stamped bfe with no card row (e.g. gc lag) -> a bare bfe feature, no geometry
    geo = _Geo(ejendom=[], codes={"a": "100412345"}, jordstykker={"a": "1000123"})
    result = Result(query="q", matches=(_addr("Hovedgaden", "8000", "x", "a"),))
    res = await project_address(result, "ejendom", geo, normalize=_norm, query="x")
    m = res.matches[0]
    assert m.feature.kind is FeatureKind.EJENDOM
    assert m.feature.name == "100412345"
    assert m.feature.components == {"bfe": "100412345"}
    assert m.feature.geometry is None


async def test_project_ejendom_unstamped_abstains() -> None:
    geo = _Geo(ejendom=[_eg()], codes={})
    result = Result(query="q", matches=(_addr("Hovedgaden", "8000", "x", "a"),))
    res = await project_address(result, "ejendom", geo, normalize=_norm, query="x")
    assert res.matches == ()  # no addresses.ejendom_bfe stamp -> projection abstains


async def test_search_one_code_lookup_uses_raw_padded_code() -> None:
    # codes store zero-padded (kommunekode 0101); normalize strips leading zeros, so the digit
    # branch must look up the raw value, not the folded one
    geo = _Geo(areas=[AreaGeom("0101", "København", 1.0, '{"type":"Polygon"}')])
    res = await search_one("kommune", "0101", geo, limit=5, normalize=lambda s: s.lstrip("0"))
    assert geo.calls == [("code", "kommune", "0101")]  # raw padded code, not "101"
    assert res.matches[0].feature.name == "København"


def _cand(aid: str, conf: Confidence) -> ResolvedAddress:
    cand = Candidate(
        address_id=aid, street="Hovedgaden", house_number="1", postcode="8000", city="x"
    )
    return ResolvedAddress(candidate=cand, confidence=conf)


async def test_project_lone_candidate_defers_to_resolution_confidence() -> None:
    # one resolved candidate has no corroboration; its projection must not claim A when the
    # resolution itself was uncertain
    geo = _Geo(areas=[AreaGeom("0751", "Aarhus", 1.0, '{"type":"Polygon"}')], codes={"a": "0751"})
    result = Result(query="q", matches=(_cand("a", Confidence.C),))
    res = await project_address(result, "kommune", geo, normalize=_norm, query="x")
    assert res.matches[0].confidence is Confidence.C  # shaky resolution -> not over-claimed as A


def test_search_cache_token_distinguishes_padded_codes() -> None:
    # the bug: normalize strips leading zeros, so 0101 and 101 folded together in the cache key and
    # an empty miss for one served the other. the token keeps codes raw -> distinct entries.
    strip0 = lambda s: s.lstrip("0")  # noqa: E731 - normalize-shaped fold for the test
    assert search_cache_token("kommune", "0101", strip0) != search_cache_token(
        "kommune", "101", strip0
    )
    assert search_cache_token("kommune", "0101", strip0) == "0101"  # raw, not folded
    assert search_cache_token("ejendom", "00420", strip0) == "00420"  # digit token stays raw


def test_search_cache_token_folds_names() -> None:
    # names still fold (case/whitespace) so the cache shares them, as area_by_name does
    assert search_cache_token("sogn", "Vor Frue", str.lower) == search_cache_token(
        "sogn", "vor frue", str.lower
    )
    # an ejendom betegnelse folds too (it no longer short-circuits to raw like a bfe)
    assert search_cache_token("ejendom", "162 Vestervold", str.lower) == "162 vestervold"


async def test_search_one_non_ascii_digits_take_name_branch() -> None:
    # str.isdigit() is true for unicode digits; they are not codes -> fuzzy name lookup, not by_code
    geo = _Geo(areas=[AreaGeom(None, "x", 0.5, None)])
    await search_one("kommune", "١٠٠", geo, limit=5, normalize=_norm)  # arabic-indic 100
    assert geo.calls[0][0] == "name"  # routed to the name path, not ("code", ...)


async def test_project_unstamped_top_does_not_inflate_confidence() -> None:
    # best candidate is ungeocoded (no code); only a lower, uncertain candidate is stamped -> the
    # lone stamped value has no corroboration and must carry that candidate's own confidence
    geo = _Geo(areas=[AreaGeom("0751", "Aarhus", 1.0, '{"type":"Polygon"}')], codes={"b": "0751"})
    result = Result(query="q", matches=(_cand("a", Confidence.A), _cand("b", Confidence.C)))
    res = await project_address(result, "kommune", geo, normalize=_norm, query="x")
    assert res.matches[0].confidence is Confidence.C  # only "b" stamped -> its own confidence
