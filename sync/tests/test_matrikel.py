"""matrikel pure ports (no db): the lodflade polygon merge + betegnelse folding.
mirrors the retired registry._mat_geometries + load._matrikel_record.
"""

from __future__ import annotations

import json

from bifrost.arms.normalize import normalize
from bifrost_sync.snapshot.matrikel import matrikel_labels, merge_polygons

_P1 = '{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}'
_P2 = '{"type":"Polygon","coordinates":[[[2,2],[3,2],[3,3],[2,2]]]}'
_MP = '{"type":"MultiPolygon","coordinates":[[[[4,4],[5,4],[5,5],[4,4]]]]}'


def test_merge_single_polygon_stays_polygon():
    merged = json.loads(merge_polygons([_P1]))
    assert merged["type"] == "Polygon"
    assert merged["coordinates"] == [[[0, 0], [1, 0], [1, 1], [0, 0]]]


def test_merge_many_polygons_concatenates_into_multipolygon():
    merged = json.loads(merge_polygons([_P1, _P2]))
    assert merged["type"] == "MultiPolygon"
    assert len(merged["coordinates"]) == 2  # each parcel polygon's rings kept as a part


def test_merge_expands_a_multipolygon_lodflade():
    # a Polygon + a MultiPolygon -> two parts (the multipolygon's polygons spliced in, not nested)
    merged = json.loads(merge_polygons([_P1, _MP]))
    assert merged["type"] == "MultiPolygon"
    assert len(merged["coordinates"]) == 2


def test_merge_none_and_malformed():
    assert merge_polygons(None) is None
    assert merge_polygons([]) is None
    assert merge_polygons(["", "not json"]) is None  # all unparseable -> geometry-less parcel


def test_matrikel_labels_betegnelse_and_folded():
    betegnelse, folded = matrikel_labels("7000k", "Vium By, Vium", "1234")
    assert betegnelse == "7000k Vium By, Vium"  # "<matrikelnummer> <ejerlavsnavn>" display
    assert folded == normalize("7000k Vium By, Vium 1234")  # folds all three into one label


def test_matrikel_labels_partial_fields():
    assert matrikel_labels("7000k", None, None) == ("7000k", normalize("7000k"))
    assert matrikel_labels(None, None, None) == (None, None)  # no label at all
