"""road record port (no db): vejnavn folds to a street_id via StreetIds; a street with no address in
the fact is an orphan, dropped. mirrors the retired load._road_record orphan drop.
"""

from __future__ import annotations

from bifrost.arms.normalize import normalize
from bifrost_sync.snapshot.records import StreetIds, to_record
from bifrost_sync.snapshot.roads import _ROAD_SQL, road_tuple


def _ids_with(*streets: str) -> StreetIds:
    ids = StreetIds()
    for s in streets:  # only streets that reached the address fact get an id
        to_record({"id": "a", "street_name": s, "house_number": 1}, ids)
    return ids


def test_road_tuple_folds_name_and_carries_sorted_postcodes():
    ids = _ids_with("Tejnvej")
    rec = road_tuple(
        {
            "navngivenvej_id": "v1",
            "vejnavn": "Tejnvej",
            "geometry": '{"type":"LineString","coordinates":[[0,0],[1,1]]}',
            "postcodes": ["3770", "3700"],
            "lifecycle": "current",
        },
        ids,
    )
    assert rec is not None
    assert rec[0] == "v1"
    assert rec[1] == ids.lookup(normalize("Tejnvej"))  # collapsed to the street's dense id
    assert rec[2] == ["3700", "3770"]  # postcodes sorted
    assert rec[3].startswith('{"type":"LineString"')  # geometry carried verbatim
    assert rec[4] == "current"  # lifecycle from the navngivenvej status


def test_road_tuple_drops_orphan_street():
    ids = _ids_with("Tejnvej")  # "Ukendtvej" never appeared in the address fact
    rec = road_tuple(
        {
            "navngivenvej_id": "v9",
            "vejnavn": "Ukendtvej",
            "geometry": "{}",
            "postcodes": ["9999"],
            "lifecycle": "current",
        },
        ids,
    )
    assert rec is None  # orphan: no address -> no street_id -> not served


def test_road_sql_qualifies_staging_and_geometry_is_optional():
    sql = _ROAD_SQL.format(staging="datafordeler")
    assert '"datafordeler".dar_navngivenvej nv' in sql
    assert '"datafordeler".dar_husnummer h' in sql
    assert (
        "nv.geometry IS NOT NULL" not in sql
    )  # geometry is nullable now (a retired road may lack it)
    assert "array_agg(DISTINCT pn.postnr)" in sql  # postcodes aggregated per road
