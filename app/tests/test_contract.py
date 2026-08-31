"""contract mapper: core Resolution -> unified wire shape. one Match type spans address + feature
depths via `kind`; geojson geometry; confidence; opt-ins."""

import pytest
from _wire_cases import EJENDOM_COMPONENTS
from bifrost.api.contract import (
    AddressRequest,
    AddressResult,
    Geometry,
    Match,
    Meta,
    ResolveItem,
    SearchItem,
    SearchRequest,
    to_result,
)
from bifrost.core.types import (
    TOP_K,
    Candidate,
    Confidence,
    EjendomInfo,
    EjendomType,
    Feature,
    FeatureKind,
    FeatureResult,
    PropertyRef,
    Resolution,
    ResolvedAddress,
    ResolvedFeature,
    Result,
)
from pydantic import ValidationError


def _ejendom_info(**kw) -> EjendomInfo:
    base = dict(
        bfe="100412345",
        type=EjendomType.EJERLEJLIGHED,
        parents=(PropertyRef("100400001", EjendomType.SAMLET_FAST_EJENDOM),),
        parents_complete=True,
        children=(),
        ejerlejlighedsnummer="4",
    )
    return EjendomInfo(**{**base, **kw})


def _cand(**kw) -> Candidate:
    base = {
        "address_id": "a1",
        "street": "Randersgade",
        "house_number": "48",
        "postcode": "2100",
        "city": "København Ø",
    }
    return Candidate(**{**base, **kw})


def _map_addr(cand, *, geometry=False, uuid=False, confidence=Confidence.A):
    ra = ResolvedAddress(candidate=cand, confidence=confidence)
    res = Resolution(address=Result(query="q", matches=(ra,)))
    return to_result("q", res, geometry=geometry, uuid=uuid, limit=TOP_K)["matches"][0]


def _map_feat(feature, *, geometry=True, confidence=Confidence.A):
    rf = ResolvedFeature(feature=feature, confidence=confidence)
    res = Resolution(feature=FeatureResult(query="q", matches=(rf,)))
    return to_result("q", res, geometry=geometry, uuid=False, limit=TOP_K)["matches"][0]


def test_wire_dicts_conform_to_response_models():
    # openapi models + hand-built dicts: two untied sources of one wire shape; pin the drift
    cand = _cand(adgangspunkt_x=1.0, adgangspunkt_y=2.0, vejpunkt_x=3.0, vejpunkt_y=4.0, score=0.9)
    ra = ResolvedAddress(candidate=cand, confidence=Confidence.B)
    addr = to_result(
        "q",
        Resolution(address=Result(query="q", matches=(ra,))),
        geometry=True,
        uuid=True,
        limit=TOP_K,
    )
    feat = Feature(
        FeatureKind.STREET,
        "Randersgade",
        1.0,
        {"street": "Randersgade"},
        '{"type":"LineString"}',
        ("2100",),
    )
    rf = ResolvedFeature(feature=feat, confidence=Confidence.A)
    feature = to_result(
        "q",
        Resolution(feature=FeatureResult(query="q", matches=(rf,))),
        geometry=True,
        uuid=False,
        limit=TOP_K,
    )
    ejf = Feature(
        FeatureKind.EJENDOM,
        "1a Byrum By, ejerlejlighed 4",
        1.0,
        EJENDOM_COMPONENTS,
        '{"type":"MultiPolygon"}',
        ejendom=_ejendom_info(),
    )
    ejendom = to_result(
        "q",
        Resolution(feature=FeatureResult(query="q", matches=(ResolvedFeature(ejf, Confidence.A),))),
        geometry=True,
        uuid=False,
        limit=TOP_K,
    )
    for result in (addr, feature, ejendom):
        AddressResult.model_validate(
            result
        )  # types + required fields present, incl the ejendom block
        for m in result["matches"]:
            # `ejendom` is the one optional key: present for ejendom kinds, absent otherwise
            assert set(m) - {"ejendom"} == set(Match.model_fields) - {"ejendom"}
            assert set(m["geometry"]) == set(Geometry.model_fields)
            assert set(m["meta"]) == set(Meta.model_fields)


# ---- address depth ----


def test_address_kind_components_and_no_geometry_by_default() -> None:
    m = _map_addr(_cand())
    assert m["kind"] == "address"
    assert m["components"] == {
        "street": "Randersgade",
        "house_number": "48",
        "postcode": "2100",
        "city": "København Ø",
    }
    assert m["meta"]["uuid"] is None
    assert m["geometry"] is None  # geometry not requested


def test_empty_city_omitted_from_components() -> None:
    assert "city" not in _map_addr(_cand(city=""))["components"]  # "" fallback -> absent, not empty


def test_confidence_always_a_letter() -> None:
    assert _map_addr(_cand(), confidence=Confidence.A)["meta"]["confidence"] == "A"
    assert _map_addr(_cand(), confidence=Confidence.B)["meta"]["confidence"] == "B"
    assert _map_addr(_cand(), confidence=Confidence.C)["meta"]["confidence"] == "C"


def test_uuid_optin_adds_the_dar_id() -> None:
    assert _map_addr(_cand(), uuid=True)["meta"]["uuid"] == "a1"


def test_address_geometry_is_geojson_point() -> None:
    g = _map_addr(_cand(adgangspunkt_x=722345.67, adgangspunkt_y=6179535.68), geometry=True)[
        "geometry"
    ]
    assert g["srid"] == 25832
    assert g["geojson"] == {"type": "Point", "coordinates": [722345.67, 6179535.68]}
    assert g["vejpunkt"] is None  # no road point -> field null, pin is the access point


def test_address_geometry_emits_access_pin_and_road_point() -> None:
    # both present: geojson is the access pin, vejpunkt rides alongside (routing/snap-to-road)
    g = _map_addr(
        _cand(
            adgangspunkt_x=722345.67,
            adgangspunkt_y=6179535.68,
            vejpunkt_x=722340.0,
            vejpunkt_y=6179530.0,
        ),
        geometry=True,
    )["geometry"]
    assert g["geojson"] == {"type": "Point", "coordinates": [722345.67, 6179535.68]}
    assert g["vejpunkt"] == (722340.0, 6179530.0)


def test_address_geometry_falls_back_to_vejpunkt() -> None:
    # access point ungeocoded but road point present -> pin falls back to it, vejpunkt still carried
    g = _map_addr(_cand(vejpunkt_x=722340.0, vejpunkt_y=6179530.0), geometry=True)["geometry"]
    assert g["geojson"]["coordinates"] == [722340.0, 6179530.0]
    assert g["vejpunkt"] == (722340.0, 6179530.0)


def test_address_road_point_dropped_on_half_null() -> None:
    # half-null road point dropped, but the access pin survives independently
    g = _map_addr(
        _cand(adgangspunkt_x=722345.67, adgangspunkt_y=6179535.68, vejpunkt_x=722340.0),
        geometry=True,
    )["geometry"]
    assert g["geojson"]["coordinates"] == [722345.67, 6179535.68]
    assert g["vejpunkt"] is None


def test_address_geometry_none_when_ungeocoded() -> None:
    assert _map_addr(_cand(), geometry=True)["geometry"] is None
    # a half-null point pair is not geocoded (must not emit a (float, None) coordinate)
    assert _map_addr(_cand(adgangspunkt_x=722345.67), geometry=True)["geometry"] is None


# ---- feature depth (street / area) ----


def _feat(kind, geojson, **kw) -> Feature:
    base = {"kind": kind, "name": "X", "score": 1.0, "components": {}, "geometry": geojson}
    return Feature(**{**base, **kw})


def test_feature_street_linestring() -> None:
    geo = '{"type":"LineString","coordinates":[[0.0,0.0],[1.0,1.0]]}'
    m = _map_feat(
        _feat(
            FeatureKind.STREET,
            geo,
            name="Randersgade",
            components={"street": "Randersgade"},
            postcodes=("8000", "8260"),  # the road's postcode set, spanning postcodes
        )
    )
    assert m["kind"] == "street"
    assert m["result"] == "Randersgade"
    assert m["components"] == {"street": "Randersgade"}
    assert m["postcodes"] == ["8000", "8260"]
    assert m["geometry"]["geojson"] == geo  # carried raw, spliced verbatim at render


def test_feature_area_polygon_kind() -> None:
    geo = '{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}'
    m = _map_feat(
        _feat(FeatureKind.KOMMUNE, geo, name="København", components={"kommune": "København"})
    )
    assert m["kind"] == "kommune"
    assert m["postcodes"] is None  # areas carry no postcode set
    assert m["geometry"]["geojson"] == geo


def test_feature_ejendom_emits_typed_block() -> None:
    geo = '{"type":"MultiPolygon","coordinates":[[[[0,0],[1,0],[1,1],[0,0]]]]}'
    m = _map_feat(
        _feat(
            FeatureKind.EJENDOM,
            geo,
            name="1a Byrum By, ejerlejlighed 4",
            components=EJENDOM_COMPONENTS,
            ejendom=_ejendom_info(),
        )
    )
    assert m["kind"] == "ejendom"
    assert m["components"] == EJENDOM_COMPONENTS
    assert m["postcodes"] is None
    assert m["geometry"]["geojson"] == geo
    assert m["ejendom"]["bfe"] == "100412345"
    assert m["ejendom"]["type"] == "ejerlejlighed"
    assert m["ejendom"]["ejerlejlighedsnummer"] == "4"
    assert m["ejendom"]["relations"]["parents"]["refs"] == [
        {"bfe": "100400001", "type": "samlet_fast_ejendom"},
    ]  # nearest -> ground, self excluded
    assert m["ejendom"]["relations"]["parents"]["complete"] is True
    assert m["ejendom"]["relations"]["children"]["refs"] == []


def test_feature_ejendom_omits_ejerlejlighedsnummer_for_sfe() -> None:
    m = _map_feat(
        _feat(
            FeatureKind.EJENDOM,
            '{"type":"MultiPolygon"}',
            name="1a Byrum By",
            components=EJENDOM_COMPONENTS,
            ejendom=_ejendom_info(type=EjendomType.SAMLET_FAST_EJENDOM, ejerlejlighedsnummer=None),
        )
    )
    assert "ejerlejlighedsnummer" not in m["ejendom"]  # omitted, not null


def _children_block(n: int) -> dict:
    children = tuple(PropertyRef(str(i), EjendomType.EJERLEJLIGHED) for i in range(n))
    m = _map_feat(
        _feat(
            FeatureKind.EJENDOM,
            '{"type":"MultiPolygon"}',
            name="x",
            components={"bfe": "1"},
            ejendom=_ejendom_info(children=children),
        )
    )
    return m["ejendom"]["relations"]["children"]


def test_feature_ejendom_children_below_the_cap_are_complete() -> None:
    block = _children_block(818)  # the largest real card observed; well under the cap
    assert len(block["refs"]) == 818
    assert block["complete"] is True


def test_feature_ejendom_children_truncate_at_the_cap() -> None:
    block = _children_block(1001)
    assert len(block["refs"]) == 1000  # bounds one card's response work
    assert block["complete"] is False  # shape-symmetric with parents.complete


def test_ejendom_block_absent_on_non_ejendom_matches() -> None:
    street = _map_feat(
        _feat(FeatureKind.STREET, '{"type":"LineString"}', name="X", components={"street": "X"})
    )
    assert "ejendom" not in street
    assert "ejendom" not in _map_addr(_cand())


def test_feature_geometry_off_omits_geometry() -> None:
    geo = '{"type":"Polygon","coordinates":[]}'
    assert _map_feat(_feat(FeatureKind.REGION, geo), geometry=False)["geometry"] is None


def test_feature_confidence_reported_on_a_and_b() -> None:
    geo = '{"type":"Polygon","coordinates":[]}'
    a = _map_feat(_feat(FeatureKind.SOGN, geo), confidence=Confidence.A)
    assert a["meta"]["confidence"] == "A"
    b = _map_feat(_feat(FeatureKind.SOGN, geo), confidence=Confidence.B)
    assert b["meta"]["confidence"] == "B"


# ---- request validation ----


def test_address_request_rejects_unknown_project() -> None:
    with pytest.raises(ValidationError):
        AddressRequest(query="x", project="jordstykke")  # not a /resolve project


def test_address_request_rejects_unknown_field() -> None:
    # the removed `autocomplete` field (now `project`) must 422, not be silently ignored
    with pytest.raises(ValidationError):
        AddressRequest(query="x", autocomplete=False)


def test_address_request_accepts_area_projection_targets() -> None:
    for t in ("kommune", "sogn", "region", "retskreds", "politikreds", "opstillingskreds"):
        assert AddressRequest(query="x", project=t).project == t


def test_address_request_accepts_ejendom() -> None:
    assert AddressRequest(query="x", project="ejendom").project == "ejendom"  # projection layer


def test_search_request_rejects_non_search_targets() -> None:
    with pytest.raises(ValidationError):
        SearchRequest(query="x", target="address")  # addresses are /resolve's domain
    with pytest.raises(ValidationError):
        SearchRequest(query="x", target="auto")  # auto is a /resolve altitude, not a register


def test_search_request_accepts_ejendom() -> None:
    assert SearchRequest(query="100412345", target="ejendom").target == "ejendom"


def test_search_request_accepts_city() -> None:
    assert SearchRequest(query="aarhus c", target="city").target == "city"


def test_search_request_defaults_and_limit_bounds() -> None:
    r = SearchRequest(query="vor frue", target="sogn")
    assert r.limit == 5 and r.geometry is False  # TOP_K default; geometry is opt-in
    with pytest.raises(ValidationError):
        SearchRequest(query="x", target="sogn", limit=0)
    with pytest.raises(ValidationError):
        SearchRequest(query="x", target="sogn", limit=101)


# ---- batch / per-item validation ----


def test_resolve_item_validates() -> None:
    assert ResolveItem(input="x").project == "address"  # default altitude
    assert ResolveItem(input="x").limit == 5  # TOP_K default
    with pytest.raises(ValidationError):
        ResolveItem(input="x", limit=21)
    assert ResolveItem(components={"kommune": "København"}, project="kommune")  # components-only ok
    with pytest.raises(ValidationError):
        ResolveItem()  # neither input nor components
    with pytest.raises(ValidationError):
        ResolveItem(input="x", project="jordstykke")  # not a /resolve project


def test_address_request_defaults_and_limit_bounds() -> None:
    r = AddressRequest(query="nørregade 13")
    assert r.limit == 5  # TOP_K default
    with pytest.raises(ValidationError):
        AddressRequest(query="x", limit=0)
    with pytest.raises(ValidationError):
        AddressRequest(query="x", limit=21)  # _MAX_RESOLVE_LIMIT; /search's 100 is the looser one


def test_address_request_batch_per_item_and_forbids_top_level() -> None:
    req = AddressRequest(
        query=["randersgade", ResolveItem(input="strandvej", project="auto")], geometry=False
    )
    assert isinstance(req.query, list) and req.query[1].project == "auto"  # bare str + item mix
    for top in ({"project": "auto"}, {"components": {"kommune": "x"}}, {"limit": 3}):
        with pytest.raises(ValidationError):
            AddressRequest(query=["x"], **top)  # config is per-item for a batch
    with pytest.raises(ValidationError):
        AddressRequest(query=[])  # empty batch is no query


def test_batch_geometry_allowed_only_for_address_points() -> None:
    ok = [ResolveItem(input="x"), ResolveItem(input="y", project="address")]
    assert AddressRequest(query=ok, geometry=True).geometry is True  # points stay small
    assert AddressRequest(query=["x", "y"], geometry=True).geometry is True  # bare str = address
    for project in ("auto", "region", "street"):
        with pytest.raises(ValidationError, match=f"projecting onto \\['{project}'\\]"):
            AddressRequest(query=[ResolveItem(input="x", project=project)], geometry=True)
        AddressRequest(query=[ResolveItem(input="x", project=project)], geometry=False)


def test_search_batch_never_carries_geometry() -> None:
    items = [SearchItem(input="vor frue", target="sogn")]
    assert SearchRequest(query=items).geometry is False
    SearchRequest(query=items, geometry=False)
    with pytest.raises(ValidationError, match="set geometry=false"):
        SearchRequest(query=items, geometry=True)


def test_batch_top_level_project_error_names_project() -> None:
    with pytest.raises(ValidationError, match="project"):
        AddressRequest(query=["x"], project="auto")


def test_resolve_batch_caps_summed_results() -> None:
    # the ceiling is today's legal max (batch cap x default), so a limit can never widen a batch
    ok = [ResolveItem(input=str(i), limit=20) for i in range(250)]
    assert len(AddressRequest(query=ok).query) == 250
    with pytest.raises(ValidationError, match="summed limit"):
        AddressRequest(query=[ResolveItem(input=str(i), limit=20) for i in range(251)])


def test_search_item_validates() -> None:
    assert SearchItem(input="x", target="sogn").limit == 5  # TOP_K default
    with pytest.raises(ValidationError):
        SearchItem(input="x", target="address")  # not a /search target
    with pytest.raises(ValidationError):
        SearchItem(input="x", target="sogn", limit=0)


def test_search_request_batch_per_item_and_forbids_top_level() -> None:
    req = SearchRequest(query=[SearchItem(input="vor frue", target="sogn", limit=3)])
    assert isinstance(req.query, list) and req.query[0].limit == 3
    for top in ({"target": "sogn"}, {"limit": 3}):
        with pytest.raises(ValidationError):
            SearchRequest(query=[SearchItem(input="x", target="sogn")], **top)  # per-item only
    with pytest.raises(ValidationError):
        SearchRequest(query=["vor frue"])  # bare string has no register -> items required
    with pytest.raises(ValidationError):
        SearchRequest(query=[])  # empty batch is no query


# ---- lifecycle request field ----


def test_lifecycle_defaults_to_current() -> None:
    assert AddressRequest(query="x").lifecycle == ["current"]
    assert SearchRequest(query="x", target="sogn").lifecycle == ["current"]
    assert ResolveItem(input="x").lifecycle == ["current"]
    assert SearchItem(input="x", target="sogn").lifecycle == ["current"]


def test_lifecycle_accepts_valid_subset() -> None:
    assert AddressRequest(query="x", lifecycle=["retired", "current"]).lifecycle == [
        "retired",
        "current",
    ]
    assert SearchRequest(query="x", target="ejendom", lifecycle=["abandoned"]).lifecycle == [
        "abandoned"
    ]


def test_lifecycle_rejects_empty_dup_unknown() -> None:
    # empty list, duplicates, and unknown states all 422 (never silently filtered)
    for bad in ([], ["current", "current"], ["gaeldende"], ["current", "nope"]):
        with pytest.raises(ValidationError):
            AddressRequest(query="x", lifecycle=bad)
        with pytest.raises(ValidationError):
            SearchRequest(query="x", target="sogn", lifecycle=bad)
        with pytest.raises(ValidationError):
            ResolveItem(input="x", lifecycle=bad)
        with pytest.raises(ValidationError):
            SearchItem(input="x", target="sogn", lifecycle=bad)


# ---- request ceilings ----


def test_request_input_capped() -> None:
    long = "x" * 513
    for bad in (
        lambda: AddressRequest(query=long),
        lambda: AddressRequest(query=[long]),
        lambda: ResolveItem(input=long),
        lambda: SearchRequest(query=long, target="sogn"),
        lambda: SearchItem(input=long, target="sogn"),
    ):
        with pytest.raises(ValidationError):
            bad()
    assert AddressRequest(query="x" * 512).query  # at the cap still passes


def test_batch_length_capped() -> None:
    assert len(AddressRequest(query=["x"] * 1000).query) == 1000
    with pytest.raises(ValidationError):
        AddressRequest(query=["x"] * 1001)
    with pytest.raises(ValidationError):
        SearchRequest(query=[SearchItem(input="x", target="sogn", limit=1)] * 1001)


def test_search_batch_caps_summed_results() -> None:
    # 100,000 materialized features was one legal request; the sum bounds it at _MAX_BATCH * TOP_K
    ok = [SearchItem(input=str(i), target="sogn", limit=10) for i in range(500)]
    assert len(SearchRequest(query=ok).query) == 500
    with pytest.raises(ValidationError, match="summed limit"):
        SearchRequest(query=[SearchItem(input=str(i), target="sogn", limit=100) for i in range(51)])


def test_components_reject_unknown_keys_and_long_values() -> None:
    # unknown keys are dropped by the resolver but would still key the cache -> 422, not empty
    for pinned in ({"stret": "x"}, {"nonce": "1"}):
        with pytest.raises(ValidationError, match="unknown component keys"):
            AddressRequest(components=pinned)
        with pytest.raises(ValidationError, match="unknown component keys"):
            ResolveItem(components=pinned)
    with pytest.raises(ValidationError):
        AddressRequest(components={"street": "x" * 129})


def test_components_accept_every_pin_field() -> None:
    for key in ("street", "house_number", "floor", "sub_locality", "kommune", "sogn", "region"):
        assert AddressRequest(components={key: "x"}).components == {key: "x"}


def test_batch_forbids_top_level_lifecycle() -> None:
    with pytest.raises(ValidationError, match="lifecycle"):
        AddressRequest(query=["x"], lifecycle=["retired"])
    with pytest.raises(ValidationError, match="lifecycle"):
        SearchRequest(query=[SearchItem(input="x", target="sogn")], lifecycle=["retired"])


def test_batch_lifecycle_is_per_item() -> None:
    req = AddressRequest(query=[ResolveItem(input="x", lifecycle=["retired"])])
    assert isinstance(req.query, list) and req.query[0].lifecycle == ["retired"]


# ---- Match.lifecycle (presented designation) + null geometry ----


def test_address_match_carries_lifecycle() -> None:
    assert _map_addr(_cand())["lifecycle"] == "current"  # default entity lifecycle
    assert _map_addr(_cand(lifecycle="retired"))["lifecycle"] == "retired"


def test_feature_match_carries_lifecycle() -> None:
    m = _map_feat(_feat(FeatureKind.STREET, '{"type":"LineString"}', lifecycle="retired"))
    assert m["lifecycle"] == "retired"  # e.g. matched via a retired street alias


def test_null_geometry_wires_null_not_suppressed() -> None:
    # a matched feature with no geometry still wires (geometry: null), never dropped
    m = _map_feat(_feat(FeatureKind.STREET, None), geometry=True)
    assert m["geometry"] is None
    assert m["kind"] == "street"  # the match itself survives the null geometry
