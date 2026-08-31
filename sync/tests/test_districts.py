"""district PIP ports (no db): full-res scale pick + covered_by stamping over geojson polygons.
mirrors the retired train/gen/test_registry.py::test_stamp_districts_point_in_polygon.
"""

from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor

from bifrost_sync.snapshot import districts
from bifrost_sync.snapshot.districts import district_geoms, stamp_chunk


def _poly(coords: str) -> str:
    return f'{{"type":"Polygon","coordinates":[{coords}]}}'


def test_district_geoms_prefers_full_res_scale():
    full = _poly("[[0,0],[9,0],[9,9],[0,9],[0,0]]")  # bigger area
    coarse = _poly("[[0,0],[1,0],[1,1],[0,1],[0,0]]")
    geoms, codes = district_geoms(
        [
            {"code": "16", "id_namespace": "http://data.gov.dk/dagi2000k", "geometry": coarse},
            {"code": "16", "id_namespace": "http://data.gov.dk/dagi", "geometry": full},
        ]
    )
    assert codes == ["16"]  # one polygon per code, not one per generalization scale
    assert geoms[0].area > 1  # the full-res (dagi) polygon won over the coarse 1:2000k


def test_district_geoms_drops_codeless_and_geomless():
    geoms, codes = district_geoms(
        [
            {"code": "", "id_namespace": "http://data.gov.dk/dagi", "geometry": _poly("[[0,0]]")},
            {"code": "16", "id_namespace": "http://data.gov.dk/dagi", "geometry": None},
        ]
    )
    assert (geoms, codes) == ([], [])


def test_stamp_chunk_covered_by_inside_outside_and_border():
    # retskreds polygon over (0,0)-(10,10); the other two kinds absent -> None columns
    poly = _poly("[[0,0],[10,0],[10,10],[0,10],[0,0]]")
    geoms, codes = district_geoms(
        [{"code": "16", "id_namespace": "http://data.gov.dk/dagi", "geometry": poly}]
    )
    from shapely import STRtree

    trees = [(STRtree(geoms), codes), None, None]
    out: dict = {}
    stamp_chunk(trees, ["h1", "h2", "h3"], [5.0, 99.0, 0.0], [5.0, 99.0, 5.0], out)
    assert out["h1"] == ["16", None, None]  # inside -> stamped, other kinds absent
    assert "h2" not in out  # outside -> unstamped
    assert out["h3"] == ["16", None, None]  # exactly on the shared border -> covered_by keeps it


def test_stamp_chunk_tiebreak_first_polygon_wins():
    from shapely import STRtree

    # two overlapping same-kind polygons; (7,7) is covered_by both
    first = _poly("[[0,0],[10,0],[10,10],[0,10],[0,0]]")
    second = _poly("[[5,5],[15,5],[15,15],[5,15],[5,5]]")
    geoms, codes = district_geoms(
        [
            {"code": "AA", "id_namespace": "http://data.gov.dk/dagi", "geometry": first},
            {"code": "BB", "id_namespace": "http://data.gov.dk/dagi", "geometry": second},
        ]
    )
    assert codes == ["AA", "BB"]  # insertion order preserved
    trees = [(STRtree(geoms), codes), None, None]
    out: dict = {}
    stamp_chunk(trees, ["h1"], [7.0], [7.0], out)
    assert out["h1"] == [
        "AA",
        None,
        None,
    ]  # first-inserted polygon wins; sharding must preserve this


def _fixture_trees():
    from shapely import STRtree

    poly = _poly("[[0,0],[10,0],[10,10],[0,10],[0,0]]")
    geoms, codes = district_geoms(
        [{"code": "16", "id_namespace": "http://data.gov.dk/dagi", "geometry": poly}]
    )
    return [(STRtree(geoms), codes), None, None]


# points spread inside / outside / on-border of the (0,0)-(10,10) retskreds polygon
_IDS = ["h0", "h1", "h2", "h3", "h4", "h5", "h6"]
_XS = [5.0, 99.0, 0.0, 2.0, 8.0, -5.0, 10.0]
_YS = [5.0, 99.0, 5.0, 2.0, 8.0, 3.0, 10.0]


def test_stamp_shard_matches_serial():
    trees = _fixture_trees()
    serial: dict = {}
    stamp_chunk(trees, _IDS, _XS, _YS, serial)

    prev = districts._WORKER_TREES
    districts._WORKER_TREES = trees
    try:
        shards = districts._shard(_IDS, _XS, _YS, 3)  # 7 points -> 3/3/1, uneven final shard
        assert [len(s[0]) for s in shards] == [3, 3, 1]
        merged: dict = {}
        for si, sx, sy in shards:
            for hid, c0, c1, c2 in districts._stamp_shard(si, sx, sy):
                merged[hid] = [c0, c1, c2]
    finally:
        districts._WORKER_TREES = prev
    assert merged == serial


def test_stamp_shard_via_executor_matches_serial():
    trees = _fixture_trees()
    serial: dict = {}
    stamp_chunk(trees, _IDS, _XS, _YS, serial)

    prev = districts._WORKER_TREES
    districts._WORKER_TREES = trees  # set pre-construction so forked workers inherit it
    try:
        ctx = multiprocessing.get_context("fork")
        with ProcessPoolExecutor(max_workers=2, mp_context=ctx) as ex:
            merged: dict = {}
            for si, sx, sy in districts._shard(_IDS, _XS, _YS, 3):
                for hid, c0, c1, c2 in ex.submit(districts._stamp_shard, si, sx, sy).result():
                    merged[hid] = [c0, c1, c2]
    finally:
        districts._WORKER_TREES = prev
    assert merged == serial
