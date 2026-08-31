"""area pure ports (no db): city union grouping + the scale-rank pick expression.
mirrors the retired registry._city_records + _area_records scale preference.
"""

from __future__ import annotations

import json
import math

from bifrost_sync.snapshot.areas import (
    _AREA_SCALE_PREF,
    _RANK,
    city_polygons,
    simplify_geojson,
)


def _sq(x0: float, y0: float, x1: float, y1: float) -> str:
    return json.dumps(
        {"type": "Polygon", "coordinates": [[[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]]]}
    )


def test_city_union_groups_by_navn():
    rows = [
        {"navn": "Ebeltoft", "geometry": _sq(0, 0, 2, 2)},
        {"navn": "Ebeltoft", "geometry": _sq(2, 0, 4, 2)},  # adjacent -> unions with the first
        {"navn": "Rønde", "geometry": _sq(10, 10, 11, 11)},
    ]
    out = dict(city_polygons(rows))
    assert set(out) == {"Ebeltoft", "Rønde"}  # one city polygon per distinct navn
    eb = json.loads(out["Ebeltoft"])
    assert eb["type"] == "Polygon"  # the two adjacent squares merged into one footprint


def test_city_union_skips_codeless_and_bad_geometry():
    rows = [
        {"navn": None, "geometry": _sq(0, 0, 1, 1)},  # no navn -> dropped
        {"navn": "X", "geometry": None},  # no geometry -> dropped
    ]
    assert city_polygons(rows) == []


def test_scale_rank_prefers_500k_then_falls_back_full_res_last():
    assert _AREA_SCALE_PREF[0] == "dagi0500k"  # 1:500k served first
    assert _AREA_SCALE_PREF[-1] == "dagi"  # full-res (multi-mb) is the last-resort fallback
    # the sql ranks the id_namespace scale token by position in the pref array, unknown scales last
    assert "array_position" in _RANK
    assert "dagi0500k" in _RANK and "regexp_replace(id_namespace" in _RANK
    assert str(len(_AREA_SCALE_PREF) + 1) in _RANK  # unknown-scale sentinel rank


def _circle(cx: float, cy: float, r: float, n: int = 400) -> str:
    pts = [
        [cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)]
        for i in range(n)
    ]
    return json.dumps({"type": "Polygon", "coordinates": [pts + [pts[0]]]})


def test_simplify_scales_tolerance_with_size_not_a_fixed_metre_value():
    small, big = _circle(0, 0, 100), _circle(0, 0, 100_000)
    for raw in (small, big):
        out = json.loads(simplify_geojson(raw))
        keep = len(out["coordinates"][0]) / len(json.loads(raw)["coordinates"][0])
        assert 0.1 < keep < 0.9  # a fixed tolerance would flatten one and skip the other


def test_simplify_preserves_every_part_of_a_multipolygon():
    tiny = json.loads(_sq(0, 0, 1, 1))["coordinates"]
    huge = json.loads(_sq(1000, 1000, 9000, 9000))["coordinates"]
    multi = json.dumps({"type": "MultiPolygon", "coordinates": [huge, tiny]})
    out = json.loads(simplify_geojson(multi))
    assert len(out["coordinates"]) == 2  # a sub-tolerance part survives, never dropped


def test_simplify_passes_through_non_polygons_and_bad_input():
    line = '{"type":"LineString","coordinates":[[0,0],[1,1]]}'
    assert simplify_geojson(line) == line  # zero area -> untouched
    assert simplify_geojson("not json") == "not json"  # fail soft, never sinks the areas pass
