"""api-layer tests: response cache round-trip + fail-open, and the endpoint dispatch path.
no DB / redis / artifact - the cache uses a dict stub and the endpoint stubs resolve_request."""

import asyncio
import json
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from _wire_cases import EJENDOM_COMPONENTS, EJENDOM_INFO
from bifrost.api import main
from bifrost.api.cache import ResponseCache
from bifrost.api.limits import LimitsMiddleware
from bifrost.core.types import (
    Candidate,
    Confidence,
    EjendomType,
    Feature,
    FeatureKind,
    FeatureResult,
    PropertyRef,
    Resolution,
    ResolvedAddress,
    ResolvedFeature,
    Result,
    rank_width,
)
from fastapi import HTTPException


def _resolution(query: str) -> Resolution:
    cand = Candidate(
        address_id="a1", street="nørregade", house_number="13", postcode="1165", city="københavn"
    )
    ra = ResolvedAddress(candidate=cand, confidence=Confidence.B)
    return Resolution(address=Result(query=query, matches=(ra,)))


class _StubRedis:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.gets = 0
        self.mgets = 0

    async def get(self, k):
        self.gets += 1
        return self.store.get(k)

    async def set(self, k, v, ex=None):
        self.store[k] = v

    async def mget(self, keys):
        self.mgets += 1
        return [self.store.get(k) for k in keys]

    async def aclose(self): ...


class _BoomRedis:
    async def get(self, k):
        raise RuntimeError("down")

    async def set(self, k, v, ex=None):
        raise RuntimeError("down")


async def test_cache_miss_then_hit_roundtrips():
    cache = ResponseCache(_StubRedis(), ttl=60)
    assert await cache.get("k") is None  # cold -> miss
    await cache.set("k", _resolution("q"))
    back = await cache.get("k")
    assert back == _resolution("q")  # frozen-dataclass eq through json bytes
    assert back.address.matches[0].candidate.floor is None  # none arms survive the round-trip
    assert back.address.matches[0].confidence is Confidence.B  # strenum survives


async def test_cache_fail_open():
    cache = ResponseCache(_BoomRedis(), ttl=60)
    assert await cache.get("k") is None  # error swallowed -> miss
    await cache.set("k", _resolution("q"))  # error swallowed -> no raise


_SEEDED_AT = datetime(2026, 8, 12, 3, 14, 22, 987654, tzinfo=UTC)
_SEEDED_HEADER = "2026-08-12T03:14:22Z"  # utc, seconds; sub-second precision is dropped


class _StubSnapshot:
    def __init__(self, generation, seeded_at):
        self.generation = generation  # endpoints read snap.generation (cache namespace)
        self.seeded_at = seeded_at  # stamped onto the response as the freshness header
        self.resolution = None  # resolve_request is stubbed, so branches/merge ctx go unused

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _StubSource:
    def __init__(self, generation="g0", seeded_at=_SEEDED_AT):
        self.generation = generation
        self.seeded_at = seeded_at

    def snapshot(self):
        # reads the current generation at request time
        return _StubSnapshot(self.generation, self.seeded_at)


def _src(generation="g0", seeded_at=_SEEDED_AT):
    return _StubSource(generation, seeded_at)


def _rt(cache=None):
    return main._Runtime(cache=cache, batch_sem=asyncio.Semaphore(4), inflight={})


def _request(source, cache):
    state = SimpleNamespace(source=source, rt=_rt(cache))
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _json(resp):
    # endpoints now return _JSONResponse; decode the rendered wire bytes (geojson spliced in raw)
    return json.loads(resp.body)


def _fake_resolution(monkeypatch):
    calls = {"n": 0}

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
        calls["n"] += 1
        return _resolution(query or "")

    monkeypatch.setattr(main, "resolve_request", fake)
    return main, calls


async def test_endpoint_miss_stores_then_hit_skips_resolve(monkeypatch):
    main, calls = _fake_resolution(monkeypatch)
    req = _request(source=_src(), cache=ResponseCache(_StubRedis(), ttl=60))

    r1 = _json(await main.resolve_endpoint(main.AddressRequest(query="Nørregade 13"), req))
    assert calls["n"] == 1 and r1["query"] == "Nørregade 13"  # miss -> resolve ran

    r2 = _json(await main.resolve_endpoint(main.AddressRequest(query="Nørregade 13"), req))
    assert calls["n"] == 1  # hit -> resolve not called again
    assert r2["query"] == "Nørregade 13"


async def test_endpoint_hit_echoes_caller_raw_query(monkeypatch):
    main, calls = _fake_resolution(monkeypatch)
    req = _request(source=_src(), cache=ResponseCache(_StubRedis(), ttl=60))

    await main.resolve_endpoint(main.AddressRequest(query="abc"), req)  # populates normalize("abc")
    r = _json(await main.resolve_endpoint(main.AddressRequest(query="ABC"), req))  # same key, raw
    assert calls["n"] == 1  # 2nd call hit the cache (same normalized key)
    assert r["query"] == "ABC"  # the mapper echoes this caller's raw query, not the cached "abc"


def _resolution_n(query: str, n: int) -> Resolution:
    matches = tuple(
        ResolvedAddress(
            candidate=Candidate(
                address_id=f"a{i}",
                street="nørregade",
                house_number=str(i),
                postcode="1165",
                city="københavn",
            ),
            confidence=Confidence.B,
        )
        for i in range(n)
    )
    return Resolution(address=Result(query=query, matches=matches))


async def test_endpoint_limit_slices_one_shared_entry_below_top_k(monkeypatch):
    ks = []

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
        ks.append(limit)
        return _resolution_n(query or "", rank_width(limit))  # as resolve_request ranks it

    monkeypatch.setattr(main, "resolve_request", fake)
    req = _request(source=_src(), cache=ResponseCache(_StubRedis(), ttl=60))

    r1 = _json(await main.resolve_endpoint(main.AddressRequest(query="nørregade", limit=2), req))
    assert len(r1["matches"]) == 2 and ks == [2]  # rendered at 2, ranked and cached at TOP_K
    r2 = _json(await main.resolve_endpoint(main.AddressRequest(query="nørregade"), req))
    assert len(r2["matches"]) == 5 and ks == [2]  # same entry serves the wider default view
    r3 = _json(await main.resolve_endpoint(main.AddressRequest(query="nørregade", limit=10), req))
    assert len(r3["matches"]) == 10 and ks == [2, 10]  # past TOP_K: its own k, its own entry


async def test_endpoint_batch_per_item_limit(monkeypatch):
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
        return _resolution_n(query or "", rank_width(limit))

    monkeypatch.setattr(main, "resolve_request", fake)
    req = _request(source=_src(), cache=None)

    out = _json(
        await main.resolve_endpoint(
            main.AddressRequest(
                query=[
                    main.ResolveItem(input="a", limit=2),
                    main.ResolveItem(input="b"),
                    main.ResolveItem(input="a", limit=3),
                ]
            ),
            req,
        )
    )
    assert [len(o["matches"]) for o in out] == [2, 5, 3]  # one query, two limits, two renders


async def test_endpoint_batch_returns_positional_envelopes(monkeypatch):
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
        return _resolution(query)

    monkeypatch.setattr(main, "resolve_request", fake)
    req = _request(source=_src(), cache=None)

    out = _json(
        await main.resolve_endpoint(main.AddressRequest(query=["Nørregade 13", "boom"]), req)
    )
    assert [o["query"] for o in out] == ["Nørregade 13", "boom"]  # positional, order preserved
    assert out[0]["matches"][0]["result"]  # first item resolved
    assert out[1]["error"] == "resolution failed"  # generic envelope; raw exception text not leaked


async def test_endpoint_batch_dispatches_per_item_config(monkeypatch):
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
        # echo the dispatch args into the feature name so the positional output reflects each item
        name = f"{project}|{query}|{sorted((components or {}).items())}"
        f = Feature(FeatureKind.STREET, name, 1.0, {}, None)
        return Resolution(
            feature=FeatureResult(query="", matches=(ResolvedFeature(f, Confidence.A),))
        )

    monkeypatch.setattr(main, "resolve_request", fake)
    req = _request(source=_src(), cache=None)

    out = _json(
        await main.resolve_endpoint(
            main.AddressRequest(
                query=[
                    main.ResolveItem(input="Nørregade 13", project="address"),
                    main.ResolveItem(input="randersgade", project="auto"),
                    main.ResolveItem(components={"kommune": "København"}, project="kommune"),
                ],
                geometry=False,
            ),
            req,
        )
    )
    assert [o["query"] for o in out] == [
        "Nørregade 13",
        "randersgade",
        "",
    ]  # input echoed positionally
    assert out[0]["matches"][0]["result"] == "address|Nørregade 13|[]"
    assert out[1]["matches"][0]["result"] == "auto|randersgade|[]"
    assert out[2]["matches"][0]["result"] == "kommune|None|[('kommune', 'København')]"


async def test_endpoint_batch_dedups_on_full_item_not_just_input(monkeypatch):
    calls = {"n": 0}

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
        calls["n"] += 1
        return _resolution(query or "")

    monkeypatch.setattr(main, "resolve_request", fake)
    req = _request(source=_src(), cache=None)

    out = _json(
        await main.resolve_endpoint(
            main.AddressRequest(
                query=[
                    main.ResolveItem(input="a", project="address"),
                    main.ResolveItem(
                        input="a", project="auto"
                    ),  # same input, other project -> distinct
                    main.ResolveItem(
                        input="a", project="address"
                    ),  # exact dup of the first -> collapses
                ],
                geometry=False,
            ),
            req,
        )
    )
    assert calls["n"] == 2  # 3 items, 2 distinct (project is part of the dedup key now)
    assert len(out) == 3  # every position still filled


async def test_endpoint_components_request_maps_feature(monkeypatch):
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
        f = Feature(
            FeatureKind.KOMMUNE,
            "København",
            1.0,
            {"kommune": "København"},
            '{"type":"Polygon","coordinates":[]}',
        )
        return Resolution(
            feature=FeatureResult(query="", matches=(ResolvedFeature(f, Confidence.A),))
        )

    monkeypatch.setattr(main, "resolve_request", fake)
    req = _request(source=_src(), cache=None)

    out = _json(
        await main.resolve_endpoint(
            main.AddressRequest(components={"kommune": "Koebenhavn"}, project="auto"), req
        )
    )
    assert out["query"] == ""  # components-only: no raw query to echo
    assert out["matches"][0]["kind"] == "kommune"
    # feature geojson spliced raw at render -> decodes back to an object, not a json string
    assert out["matches"][0]["geometry"]["geojson"] == {"type": "Polygon", "coordinates": []}


async def test_endpoint_street_feature_exposes_postcodes_array(monkeypatch):
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
        f = Feature(
            FeatureKind.STREET,
            "Hovedgaden",
            1.0,
            {"street": "Hovedgaden"},
            '{"type":"LineString","coordinates":[[0,0],[1,1]]}',
            postcodes=("8000", "8260"),  # one road, the postcodes it spans
        )
        return Resolution(
            feature=FeatureResult(query="", matches=(ResolvedFeature(f, Confidence.A),))
        )

    monkeypatch.setattr(main, "resolve_request", fake)
    req = _request(source=_src(), cache=None)

    out = _json(
        await main.resolve_endpoint(
            main.AddressRequest(components={"street": "Hovedgaden"}, project="auto"), req
        )
    )
    m = out["matches"][0]
    assert m["kind"] == "street"
    assert m["postcodes"] == [
        "8000",
        "8260",
    ]  # the spanning postcode set, beside the name component
    assert m["components"] == {"street": "Hovedgaden"}
    assert m["geometry"]["geojson"] == {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}


async def test_endpoint_503_until_source_ready(monkeypatch):
    req = _request(source=None, cache=None)  # lifespan hasn't published the source yet
    with pytest.raises(HTTPException) as ei:
        await main.resolve_endpoint(main.AddressRequest(query="x"), req)
    assert ei.value.status_code == 503


async def test_ready_endpoint_reflects_source(monkeypatch):
    with pytest.raises(HTTPException) as ei:
        await main.ready(_request(source=None, cache=None))
    assert ei.value.status_code == 503  # unready until the source is published
    assert (await main.ready(_request(source=_src(), cache=None)))["status"] == "ready"


async def test_ready_endpoint_holds_through_a_failing_lease():
    # the lease write needs the primary; the gc it guards needs that same primary, so a pod that
    # resolves fine off a replica must stay in the rotation instead of taking the plane down
    src = _src()
    src.lease_stale = True  # a leftover flag must not reach the readiness decision
    assert (await main.ready(_request(source=src, cache=None)))["status"] == "ready"


async def test_health_is_pure_liveness():
    assert (await main.health())[
        "status"
    ] == "ok"  # never 503: unseeded is transient-unready, not dead


def test_cache_key_namespaced_by_generation():
    lc = ("current",)
    ka = main._cache_key("genA", "Nørregade 13", None, "address", lc, 5)
    kb = main._cache_key("genB", "Nørregade 13", None, "address", lc, 5)
    assert ka != kb  # same query, different generation -> different key (no cross-gen mis-serve)
    # stable within a gen
    assert ka == main._cache_key("genA", "Nørregade 13", None, "address", lc, 5)
    sa = main._search_cache_key("genA", "sogn", "vor frue", 5, lc)
    assert sa != main._search_cache_key("genB", "sogn", "vor frue", 5, lc)


def test_cache_key_buckets_limit_at_top_k():
    # every limit <= TOP_K resolves at k=5, so they share one entry; a wider limit gets its own
    lc = ("current",)
    k = [
        main._cache_key("g", "Nørregade 13", None, "address", lc, main.rank_width(n))
        for n in (1, 3, 5)
    ]
    assert len(set(k)) == 1
    assert main._cache_key("g", "Nørregade 13", None, "address", lc, main.rank_width(10)) not in k


def test_cache_key_separates_lifecycle_sets():
    # same query, different lifecycle set -> distinct key so a retired lookup never serves a current
    cur = main._cache_key("g", "Nørregade 13", None, "address", ("current",), 5)
    ret = main._cache_key("g", "Nørregade 13", None, "address", ("retired",), 5)
    both = main._cache_key("g", "Nørregade 13", None, "address", ("current", "retired"), 5)
    assert len({cur, ret, both}) == 3
    sc = main._search_cache_key("g", "ejendom", "1a byrum", 5, ("current",))
    sr = main._search_cache_key("g", "ejendom", "1a byrum", 5, ("current", "retired"))
    assert sc != sr


async def test_endpoint_cache_recomputes_after_generation_cutover(monkeypatch):
    main, calls = _fake_resolution(monkeypatch)
    src = _src("genA")
    req = _request(source=src, cache=ResponseCache(_StubRedis(), ttl=60))

    await main.resolve_endpoint(main.AddressRequest(query="Nørregade 13"), req)
    await main.resolve_endpoint(main.AddressRequest(query="Nørregade 13"), req)
    assert calls["n"] == 1  # same generation -> cache hit
    src.generation = "genB"  # a cutover moves the namespace
    await main.resolve_endpoint(main.AddressRequest(query="Nørregade 13"), req)
    assert calls["n"] == 2  # new generation -> miss, recomputed under the new namespace


def _sogn_resolution() -> Resolution:
    f = Feature(
        FeatureKind.SOGN, "Vor Frue Sogn", 1.0, {"sogn": "Vor Frue Sogn"}, '{"type":"Polygon"}'
    )
    return Resolution(feature=FeatureResult(query="", matches=(ResolvedFeature(f, Confidence.A),)))


async def test_search_endpoint_miss_then_hit(monkeypatch):
    calls = {"n": 0}

    async def fake(query, target, *, geo_source, limit, lifecycle=("current",)):
        calls["n"] += 1
        return _sogn_resolution()

    monkeypatch.setattr(main, "search_request", fake)
    req = _request(source=_src(), cache=ResponseCache(_StubRedis(), ttl=60))

    r1 = _json(await main.search_endpoint(main.SearchRequest(query="vor frue", target="sogn"), req))
    assert calls["n"] == 1 and r1["matches"][0]["kind"] == "sogn"  # miss -> search ran
    r2 = _json(await main.search_endpoint(main.SearchRequest(query="VOR FRUE", target="sogn"), req))
    assert calls["n"] == 1  # same normalized key -> cache hit, search not re-run
    assert r2["matches"][0]["result"] == "Vor Frue Sogn"


def _ejendom_resolution() -> Resolution:
    f = Feature(
        FeatureKind.EJENDOM,
        "1a Byrum By, ejerlejlighed 4",
        1.0,
        EJENDOM_COMPONENTS,
        '{"type":"MultiPolygon"}',
        ejendom=EJENDOM_INFO,
    )
    return Resolution(feature=FeatureResult(query="", matches=(ResolvedFeature(f, Confidence.A),)))


async def test_search_endpoint_ejendom_by_bfe(monkeypatch):
    async def fake(query, target, *, geo_source, limit, lifecycle=("current",)):
        return _ejendom_resolution()

    monkeypatch.setattr(main, "search_request", fake)
    req = _request(source=_src(), cache=ResponseCache(_StubRedis(), ttl=60))
    r = _json(
        await main.search_endpoint(
            main.SearchRequest(query="100412345", target="ejendom", geometry=True), req
        )
    )
    m = r["matches"][0]
    assert m["kind"] == "ejendom"
    assert m["components"] == EJENDOM_COMPONENTS
    assert m["geometry"]["geojson"] == {"type": "MultiPolygon"}  # spliced raw
    assert m["ejendom"]["relations"]["parents"]["refs"] == [
        {"bfe": "100400001", "type": "samlet_fast_ejendom"},
    ]
    assert m["ejendom"]["ejerlejlighedsnummer"] == "4"
    assert m["ejendom"]["relations"]["children"]["refs"] == []


async def test_cache_roundtrips_ejendom_feature():
    # the rich Feature (EjendomInfo dataclass + PropertyRef namedtuples) survives the TypeAdapter
    cache = ResponseCache(_StubRedis(), ttl=60)
    res = _ejendom_resolution()
    await cache.set("ek", res)
    back = await cache.get("ek")
    assert back == res  # frozen dataclass + namedtuple refs survive the json round-trip
    e = back.feature.matches[0].feature.ejendom
    assert e.parents[0] == PropertyRef("100400001", EjendomType.SAMLET_FAST_EJENDOM)
    assert e.type is EjendomType.EJERLEJLIGHED


def _stednavne_resolution() -> Resolution:
    f = Feature(
        FeatureKind.STEDNAVNE,
        "Furesø",
        1.0,
        {"stednavne": "Furesø", "type": "sø"},
        '{"type":"Polygon"}',
    )
    return Resolution(feature=FeatureResult(query="", matches=(ResolvedFeature(f, Confidence.A),)))


async def test_search_endpoint_stednavne(monkeypatch):
    async def fake(query, target, *, geo_source, limit, lifecycle=("current",)):
        return _stednavne_resolution()

    monkeypatch.setattr(main, "search_request", fake)
    req = _request(source=_src(), cache=ResponseCache(_StubRedis(), ttl=60))
    r = _json(
        await main.search_endpoint(
            main.SearchRequest(query="furesø", target="stednavne", geometry=True), req
        )
    )
    assert r["matches"][0]["kind"] == "stednavne"
    assert r["matches"][0]["components"] == {"stednavne": "Furesø", "type": "sø"}
    assert r["matches"][0]["geometry"]["geojson"] == {"type": "Polygon"}  # spliced raw


async def test_search_endpoint_batch_per_item(monkeypatch):
    async def fake(query, target, *, geo_source, limit, lifecycle=("current",)):
        if query == "boom":
            raise ValueError("bad")
        f = Feature(FeatureKind(target), f"{target}|{query}|{limit}", 1.0, {}, None)
        return Resolution(
            feature=FeatureResult(query="", matches=(ResolvedFeature(f, Confidence.A),))
        )

    monkeypatch.setattr(main, "search_request", fake)
    req = _request(source=_src(), cache=None)

    out = _json(
        await main.search_endpoint(
            main.SearchRequest(
                query=[
                    main.SearchItem(input="vor frue", target="sogn"),
                    main.SearchItem(input="100412345", target="ejendom", limit=3),
                    main.SearchItem(input="boom", target="sogn"),
                ]
            ),
            req,
        )
    )
    assert [o["query"] for o in out] == ["vor frue", "100412345", "boom"]  # positional
    assert out[0]["matches"][0]["result"] == "sogn|vor frue|5"  # default limit per item
    assert out[1]["matches"][0]["result"] == "ejendom|100412345|3"  # per-item limit honored
    assert out[2]["error"] == "search failed"  # per-item isolation, generic envelope


async def test_search_endpoint_503_until_source_ready(monkeypatch):
    req = _request(source=None, cache=None)
    with pytest.raises(HTTPException) as ei:
        await main.search_endpoint(main.SearchRequest(query="x", target="sogn"), req)
    assert ei.value.status_code == 503


def test_json_response_renders_geojson_verbatim():
    content = {
        "query": "Søndergård Allé",
        "matches": [
            {
                "result": "Æblevej",
                "postcodes": None,  # exclude_none off: nulls stay
                "geometry": {
                    "geojson": {"type": "Point", "coordinates": [722.1, 6177.2]},
                    "vejpunkt": (722.0, 6177.0),  # tuple -> json array
                },
            }
        ],
    }
    body = main._JSONResponse(content=content).body
    out = json.loads(body)
    assert out["query"] == "Søndergård Allé"
    assert out["matches"][0]["geometry"]["geojson"]["type"] == "Point"
    assert out["matches"][0]["geometry"]["vejpunkt"] == [722.0, 6177.0]
    assert out["matches"][0]["postcodes"] is None


def test_json_response_splices_feature_geojson_raw():
    # feature geometry arrives as pre-serialized geojson text; render splices it verbatim (not
    # double-encoded as a json string) without parsing
    geo = '{"type":"MultiPolygon","coordinates":[[[[0,0],[1,0],[1,1],[0,0]]]]}'
    content = {"query": "q", "matches": [{"result": "x", "geometry": {"geojson": geo}}]}
    body = main._JSONResponse(content=content).body
    assert geo.encode() in body  # spliced raw, exactly once
    assert body.count(geo.encode()) == 1
    out = json.loads(body)  # still valid json
    assert out["matches"][0]["geometry"]["geojson"] == json.loads(geo)  # an object, not a string


def test_json_response_splices_batch_geojson_without_collision():
    # per-feature tokens must not collide across a batch list
    a = '{"type":"Point","coordinates":[1,2]}'
    b = '{"type":"LineString","coordinates":[[0,0],[3,4]]}'
    content = [
        {"query": "a", "matches": [{"result": "x", "geometry": {"geojson": a}}]},
        {"query": "b", "matches": [{"result": "y", "geometry": {"geojson": b}}]},
    ]
    out = json.loads(main._JSONResponse(content=content).body)
    assert out[0]["matches"][0]["geometry"]["geojson"] == json.loads(a)
    assert out[1]["matches"][0]["geometry"]["geojson"] == json.loads(b)


def test_json_response_splices_one_dict_aliased_at_two_positions():
    # dedup hands the same result dict to both positions; the splice is keyed on id, not value
    geo = '{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}'
    shared = {"geojson": geo}
    content = [{"query": "q", "matches": [{"result": "x", "geometry": shared}]}] * 2
    body = main._JSONResponse(content=content).body
    out = json.loads(body)  # the defect emitted a bare sentinel token here -> JSONDecodeError
    assert [o["matches"][0]["geometry"]["geojson"] for o in out] == [json.loads(geo)] * 2


async def test_batch_mget_all_hits_skip_recompute(monkeypatch):
    main, calls = _fake_resolution(monkeypatch)
    stub = _StubRedis()
    req = _request(source=_src(), cache=ResponseCache(stub, ttl=60))

    for q in ("a", "b", "c"):  # warm each key via the single-item path
        await main.resolve_endpoint(main.AddressRequest(query=q), req)
    assert calls["n"] == 3
    gets_before = stub.gets

    out = _json(await main.resolve_endpoint(main.AddressRequest(query=["a", "b", "c"]), req))
    assert [o["query"] for o in out] == ["a", "b", "c"]  # order preserved
    assert calls["n"] == 3  # every item hit -> nothing recomputed
    assert stub.mgets == 1 and stub.gets == gets_before  # one mget, zero per-item gets on hits


async def test_batch_mget_mixed_recomputes_only_misses(monkeypatch):
    main, calls = _fake_resolution(monkeypatch)
    stub = _StubRedis()
    req = _request(source=_src(), cache=ResponseCache(stub, ttl=60))

    await main.resolve_endpoint(main.AddressRequest(query="a"), req)
    await main.resolve_endpoint(main.AddressRequest(query="b"), req)
    assert calls["n"] == 2

    out = _json(await main.resolve_endpoint(main.AddressRequest(query=["a", "c", "b", "d"]), req))
    assert [o["query"] for o in out] == ["a", "c", "b", "d"]  # order preserved across hits + misses
    assert calls["n"] == 4  # only c and d recomputed
    assert stub.mgets == 1  # a single mget fronts the batch


async def test_batch_mget_all_miss(monkeypatch):
    main, calls = _fake_resolution(monkeypatch)
    stub = _StubRedis()
    req = _request(source=_src(), cache=ResponseCache(stub, ttl=60))

    out = _json(await main.resolve_endpoint(main.AddressRequest(query=["x", "y", "z"]), req))
    assert [o["query"] for o in out] == ["x", "y", "z"]  # order preserved
    assert calls["n"] == 3  # cold cache -> all computed
    assert stub.mgets == 1
    assert stub.gets == 0  # a miss the mget already proved is never re-read per item
    assert all(o["matches"][0]["result"] for o in out)  # each position carries its own resolution


async def test_singleflight_collapses_concurrent_misses():
    cache = ResponseCache(_StubRedis(), ttl=60)
    rt = _rt(cache)
    calls = {"n": 0}
    release = asyncio.Event()

    async def factory():
        calls["n"] += 1
        await release.wait()  # hold so every concurrent caller piles onto one future
        return _resolution("q")

    tasks = [asyncio.create_task(main._cached(rt, "sf", factory)) for _ in range(5)]
    await asyncio.sleep(0)  # let the leader register before the rest become waiters
    release.set()
    out = await asyncio.gather(*tasks)
    assert calls["n"] == 1  # one compute despite five concurrent misses
    assert all(r == _resolution("q") for r in out)
    assert "sf" not in rt.inflight  # entry cleaned in finally


async def test_singleflight_follower_cancel_does_not_poison_leader():
    cache = ResponseCache(_StubRedis(), ttl=60)
    rt = _rt(cache)
    release = asyncio.Event()

    async def factory():
        await release.wait()
        return _resolution("q")

    leader = asyncio.create_task(main._cached(rt, "sf3", factory))
    await asyncio.sleep(0)  # leader registers the future
    follower = asyncio.create_task(main._cached(rt, "sf3", factory))
    for _ in range(5):
        await asyncio.sleep(0)  # follower reaches the await on the shared future
    follower.cancel()
    release.set()
    assert await leader == _resolution("q")  # cancelled follower never cancels the shared future
    assert follower.cancelled()
    assert "sf3" not in rt.inflight


async def test_singleflight_failure_unblocks_waiters_and_recovers():
    cache = ResponseCache(_StubRedis(), ttl=60)
    rt = _rt(cache)
    calls = {"n": 0}
    release = asyncio.Event()

    async def boom():
        calls["n"] += 1
        await release.wait()  # hold so waiters pile on before the raise
        raise RuntimeError("nope")

    tasks = [asyncio.create_task(main._cached(rt, "sf2", boom)) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()
    out = await asyncio.gather(*tasks, return_exceptions=True)
    assert calls["n"] == 1  # one shared compute
    assert all(isinstance(r, RuntimeError) for r in out)  # each waiter sees the error, none hang
    assert "sf2" not in rt.inflight  # not poisoned: entry cleaned

    async def ok():
        return _resolution("q")

    assert await main._cached(rt, "sf2", ok) == _resolution("q")  # later call recomputes cleanly


# ---- data-freshness header ----


async def test_freshness_header_on_every_200_path(monkeypatch):
    _fake_resolution(monkeypatch)

    async def fake_search(query, target, *, geo_source, limit, lifecycle=("current",)):
        return _sogn_resolution()

    monkeypatch.setattr(main, "search_request", fake_search)
    req = _request(source=_src(), cache=None)

    responses = [
        await main.resolve_endpoint(main.AddressRequest(query="Nørregade 13"), req),
        await main.resolve_endpoint(main.AddressRequest(query=["Nørregade 13", "Vestergade"]), req),
        await main.search_endpoint(main.SearchRequest(query="vor frue", target="sogn"), req),
        await main.search_endpoint(
            main.SearchRequest(query=[{"input": "vor frue", "target": "sogn"}]), req
        ),
    ]
    # batch bodies are bare arrays, so the header is the only shape covering all four paths
    assert [r.headers[main.DATA_UPDATED_HEADER] for r in responses] == [_SEEDED_HEADER] * 4


async def test_freshness_header_survives_a_cache_hit(monkeypatch):
    _, calls = _fake_resolution(monkeypatch)
    req = _request(source=_src(), cache=ResponseCache(_StubRedis(), ttl=60))

    await main.resolve_endpoint(main.AddressRequest(query="Nørregade 13"), req)
    r = await main.resolve_endpoint(main.AddressRequest(query="Nørregade 13"), req)
    assert calls["n"] == 1  # 2nd call served from cache
    # stamped from the pinned snapshot, not the cached Resolution, so a hit still carries it
    assert r.headers[main.DATA_UPDATED_HEADER] == _SEEDED_HEADER


async def test_freshness_header_is_utc_normalized(monkeypatch):
    _fake_resolution(monkeypatch)
    cet = datetime(2026, 8, 12, 5, 14, 22, tzinfo=timezone(timedelta(hours=2)))
    req = _request(source=_src(seeded_at=cet), cache=None)

    r = await main.resolve_endpoint(main.AddressRequest(query="Nørregade 13"), req)
    assert r.headers[main.DATA_UPDATED_HEADER] == _SEEDED_HEADER  # 05:14+02:00 -> 03:14Z


# ---- admission control (drives the asgi callable directly, like test_metrics) ----


async def _echo(scope, receive, send) -> None:
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _limits(app, **kw) -> LimitsMiddleware:
    opts = {"max_body_bytes": 1000, "max_inflight": 1, "request_timeout": 5.0} | kw
    return LimitsMiddleware(app, **opts)


async def _drive(mw, sent: list, *, path="/resolve", body=b"") -> None:
    scope = {"type": "http", "path": path, "method": "POST", "headers": []}

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await mw(scope, receive, send)


async def test_limits_413_past_the_body_ceiling():
    mw = _limits(_echo, max_body_bytes=4)
    with pytest.raises(HTTPException) as ei:  # raised into the body read, rendered as a 413
        await _drive(mw, [], body=b"xxxxx")
    assert ei.value.status_code == 413


async def test_limits_sheds_when_inflight_is_full():
    release = asyncio.Event()

    async def slow(scope, receive, send):
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = _limits(slow, max_inflight=1)
    held = asyncio.create_task(_drive(mw, []))
    await asyncio.sleep(0)  # let it take the only slot
    sent: list = []
    await _drive(mw, sent)
    assert sent[0]["status"] == 503
    assert (b"retry-after", b"1") in sent[0]["headers"]  # shed, never queued
    release.set()
    await held


async def test_limits_504_past_the_deadline():
    async def stall(scope, receive, send):
        await asyncio.Event().wait()

    sent: list = []
    await _drive(_limits(stall, request_timeout=0.01), sent)
    assert sent[0]["status"] == 504


async def test_limits_skip_probe_paths():
    sent: list = []
    await _drive(_limits(_echo, request_timeout=0.0), sent, path="/health")
    assert sent[0]["status"] == 200  # a bounded path would have 504'd on this deadline
