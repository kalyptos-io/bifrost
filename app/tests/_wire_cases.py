"""shared request and resolution fixtures for wire-serialization parity."""

from collections import namedtuple

from bifrost.core.types import (
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

Case = namedtuple("Case", "name endpoint payload fake")

# card components, ordered as core.geo._ejendom_components emits them (bfe then the matched parcel)
EJENDOM_COMPONENTS = {
    "bfe": "100412345",
    "jordstykke": "1000123",
    "matrikelnummer": "1a",
    "ejerlavskode": "60851",
    "ejerlavsnavn": "Byrum By",
    "kommunekode": "0400",
    "kommunenavn": "Bornholm",
    "centroid": "882000 6100000",
    "matrikelbetegnelse": "1a Byrum By",
}

EJENDOM_INFO = EjendomInfo(
    "100412345",
    EjendomType.EJERLEJLIGHED,
    (PropertyRef("100400001", EjendomType.SAMLET_FAST_EJENDOM),),
    True,
    (),
    "4",
)


def _cand(**kw) -> Candidate:
    base = dict(
        address_id="a1",
        street="Randersgade",
        house_number="48",
        postcode="2100",
        city="København Ø",
    )
    return Candidate(**{**base, **kw})


def _addr_res(query: str, matches: tuple[ResolvedAddress, ...]) -> Resolution:
    return Resolution(address=Result(query=query, matches=matches))


def _feat_res(query: str, matches: tuple[ResolvedFeature, ...]) -> Resolution:
    return Resolution(feature=FeatureResult(query=query, matches=matches))


def _feature(kind, name, components, geojson, *, score=1.0, postcodes=()) -> Feature:
    return Feature(kind, name, score, components, geojson, postcodes)


# --- individual resolutions ---


def _res_addr_point() -> Resolution:
    c = _cand(
        score=0.9712,
        adgangspunkt_x=722345.67,
        adgangspunkt_y=6179535.68,
        vejpunkt_x=722340.0,
        vejpunkt_y=6179530.0,
    )
    ra = ResolvedAddress(c, Confidence.B)
    return _addr_res("Randrsgade 48", (ra,))


def _res_addr_clean_multi() -> Resolution:
    c1 = _cand(score=0.9801, adgangspunkt_x=722345.67, adgangspunkt_y=6179535.68)
    c2 = _cand(address_id="a2", house_number="50", city="", score=0.8802)
    m1 = ResolvedAddress(c1, Confidence.A)
    m2 = ResolvedAddress(c2, Confidence.C)
    return _addr_res("Randersgade", (m1, m2))


def _res_abstain() -> Resolution:
    return _addr_res("qwxz", ())


def _res_feature_kommune() -> Resolution:
    f = _feature(
        FeatureKind.KOMMUNE,
        "København",
        {"kommune": "København"},
        '{"type":"Polygon","coordinates":[[[722.1,6177.2],[723.0,6177.0],[722.1,6177.2]]]}',
    )
    return _feat_res("koebenhavn", (ResolvedFeature(f, Confidence.A),))


def _res_feature_multi() -> Resolution:
    a = _feature(
        FeatureKind.STREET,
        "Randersgade",
        {"street": "Randersgade"},
        '{"type":"LineString","coordinates":[[0.0,0.0],[1.5,2.5]]}',
        score=0.97,
        postcodes=("2100",),
    )
    b = _feature(
        FeatureKind.STREET,
        "Randersvej",
        {"street": "Randersvej"},
        '{"type":"LineString","coordinates":[[3.0,3.0],[4.0,4.0]]}',
        score=0.81,
        postcodes=("8900", "8940"),
    )
    return _feat_res(
        "randers", (ResolvedFeature(a, Confidence.C), ResolvedFeature(b, Confidence.C))
    )


def _res_sogn() -> Resolution:
    f = _feature(
        FeatureKind.SOGN,
        "Vor Frue Sogn",
        {"sogn": "Vor Frue Sogn"},
        '{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}',
    )
    return _feat_res("", (ResolvedFeature(f, Confidence.A),))


def _res_ejendom() -> Resolution:
    f = Feature(
        FeatureKind.EJENDOM,
        "1a Byrum By, ejerlejlighed 4",
        1.0,
        EJENDOM_COMPONENTS,
        '{"type":"MultiPolygon","coordinates":[[[[0,0],[1,0],[1,1],[0,0]]]]}',
        ejendom=EJENDOM_INFO,
    )
    return _feat_res("", (ResolvedFeature(f, Confidence.A),))


# --- fakes ---


def _const_resolve(res: Resolution):
    async def fake(
        query,
        components,
        *,
        project,
        source,
        geo_source,
        resolution,
        lifecycle=("current",),
        limit=5,
    ):
        return res

    return fake


def _const_search(res: Resolution):
    async def fake(query, target, *, geo_source, limit, lifecycle=("current",)):
        return res

    return fake


def _batch_resolve(mapping: dict[str, Resolution]):
    async def fake(
        query,
        components,
        *,
        project,
        source,
        geo_source,
        resolution,
        lifecycle=("current",),
        limit=5,
    ):
        if query == "boom":
            raise ValueError("bad")
        return mapping[query]

    return fake


def _batch_search(builder):
    async def fake(query, target, *, geo_source, limit, lifecycle=("current",)):
        if query == "boom":
            raise ValueError("bad")
        return builder(query, target)

    return fake


def _search_builder(query, target):
    if target == "sogn":
        return _res_sogn()
    return _res_ejendom()


def cases() -> list[Case]:
    return [
        Case(
            "resolve_addr_geom_on_uuid_off",
            "resolve",
            {"query": "Randrsgade 48", "geometry": True, "uuid": False},
            _const_resolve(_res_addr_point()),
        ),
        Case(
            "resolve_addr_geom_on_uuid_on",
            "resolve",
            {"query": "Randrsgade 48", "geometry": True, "uuid": True},
            _const_resolve(_res_addr_point()),
        ),
        Case(
            "resolve_addr_geom_off",
            "resolve",
            {"query": "Randrsgade 48", "geometry": False, "uuid": True},
            _const_resolve(_res_addr_point()),
        ),
        Case(
            "resolve_addr_clean_multi",
            "resolve",
            {"query": "Randersgade", "geometry": True, "uuid": False},
            _const_resolve(_res_addr_clean_multi()),
        ),
        Case(
            "resolve_addr_limit_one",
            "resolve",
            {"query": "Randersgade", "geometry": True, "uuid": False, "limit": 1},
            _const_resolve(_res_addr_clean_multi()),
        ),
        Case(
            "resolve_abstain_empty",
            "resolve",
            {"query": "qwxz", "geometry": True, "uuid": True},
            _const_resolve(_res_abstain()),
        ),
        Case(
            "resolve_feature_single_geojson",
            "resolve",
            {"components": {"kommune": "Koebenhavn"}, "project": "kommune", "geometry": True},
            _const_resolve(_res_feature_kommune()),
        ),
        Case(
            "resolve_feature_multi_geojson",
            "resolve",
            {"query": "randers", "project": "auto", "geometry": True},
            _const_resolve(_res_feature_multi()),
        ),
        Case(
            "resolve_feature_geom_off",
            "resolve",
            {"components": {"kommune": "Koebenhavn"}, "project": "kommune", "geometry": False},
            _const_resolve(_res_feature_kommune()),
        ),
        Case(
            "resolve_batch_mixed",
            "resolve",
            {"query": ["Randrsgade 48", "randers", "boom"], "geometry": True, "uuid": True},
            _batch_resolve({"Randrsgade 48": _res_addr_point(), "randers": _res_feature_multi()}),
        ),
        Case(
            "resolve_batch_geom_off",
            "resolve",
            {"query": ["Randrsgade 48", "koebenhavn"], "geometry": False},
            _batch_resolve(
                {"Randrsgade 48": _res_addr_point(), "koebenhavn": _res_feature_kommune()}
            ),
        ),
        Case(
            "search_single_geojson",
            "search",
            {"query": "vor frue", "target": "sogn", "geometry": True},
            _const_search(_res_sogn()),
        ),
        Case(
            "search_geom_off",
            "search",
            {"query": "vor frue", "target": "sogn", "geometry": False},
            _const_search(_res_sogn()),
        ),
        Case(
            "search_batch_mixed",
            "search",
            {
                "query": [
                    {"input": "vor frue", "target": "sogn"},
                    {"input": "100412345", "target": "ejendom", "limit": 3},
                    {"input": "boom", "target": "sogn"},
                ],
                "geometry": False,  # a batch never carries geometry
            },
            _batch_search(_search_builder),
        ),
    ]
