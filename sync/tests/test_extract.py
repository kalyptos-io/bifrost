"""zip csv streaming, currency predicates, wkt->geojson, sniffers, and shape_row projection/rename.

wkt + sniffer + predicate cases ported from train/gen/test_registry.py; dialect-rename, keep-list
projection, and version-mix tolerance are new.
"""

from __future__ import annotations

import csv
import io
import zipfile

import pytest
from bifrost_sync import registers
from bifrost_sync.extract import (
    SniffState,
    _wkt_to_geojson,
    pk_value,
    shape_row,
    to_utc_iso,
    zip_rows,
)
from bifrost_sync.registers import ALL_ENTITIES, Column, Currency, EntitySpec, Kind


def _write_entity_zip(path: str, rows: list[dict], *, delim: str = ",") -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), delimiter=delim)
    w.writeheader()
    w.writerows(rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("data.csv", buf.getvalue().encode("utf-8"))


def _spec(table: str) -> EntitySpec:
    return next(s for s in ALL_ENTITIES if s.table == table)


def _shape(spec: EntitySpec, row: dict) -> dict:
    return shape_row(row, spec, SniffState(spec))


# to_utc_iso: one canonical utc form so lexical order matches chronological order


def test_to_utc_iso_canonicalizes_offsets_and_z():
    # +01:00 (CET) and +02:00 (CEST) collapse to a common utc form; lexical == chronological
    cet = to_utc_iso("2020-01-01T12:00:00+01:00")
    cest = to_utc_iso("2020-06-01T12:00:00+02:00")
    assert cet == "2020-01-01T11:00:00+00:00"
    assert to_utc_iso("2020-01-01T00:00:00Z") == "2020-01-01T00:00:00+00:00"
    assert to_utc_iso("2010-01-01") == "2010-01-01T00:00:00+00:00"  # naive date -> utc midnight
    assert cest > cet  # june noon CEST is later than january noon CET
    assert to_utc_iso("") is None and to_utc_iso("not-a-date") is None


# zip csv streaming


def test_zip_rows_sniffs_semicolon(tmp_path):
    p = str(tmp_path / "x.zip")
    _write_entity_zip(p, [{"id_lokalId": "v1", "vejnavn": "A;B er ok", "status": "3"}], delim=";")
    rows = list(zip_rows(p))
    assert rows[0]["id_lokalId"] == "v1" and rows[0]["vejnavn"] == "A;B er ok"


def test_zip_rows_picks_csv_member_over_other_files(tmp_path):
    p = str(tmp_path / "x.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", b"metadata first, not the data")
        z.writestr("data.csv", b"id_lokalId,vejnavn,status\nv1,Vestergade,3\n")
    assert list(zip_rows(p))[0]["vejnavn"] == "Vestergade"


def test_zip_rows_raises_without_csv_member(tmp_path):
    p = str(tmp_path / "x.zip")
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("readme.txt", b"no csv here")
    with pytest.raises(SystemExit):
        list(zip_rows(p))


# wkt -> geojson (ported)


def test_wkt_point():
    assert _wkt_to_geojson("POINT(722345.67 6179535.68)") == {
        "type": "Point",
        "coordinates": [722345.67, 6179535.68],
    }


def test_wkt_linestring_and_z_dropped():
    assert _wkt_to_geojson("LINESTRING(0 0, 1 1)") == {
        "type": "LineString",
        "coordinates": [[0.0, 0.0], [1.0, 1.0]],
    }
    # the dar form: "MULTILINESTRING Z((x y z, ...))" - the z ordinate is dropped
    assert _wkt_to_geojson("MULTILINESTRING Z((0 0 5, 1 1 6),(2 2 7, 3 3 8))") == {
        "type": "MultiLineString",
        "coordinates": [[[0.0, 0.0], [1.0, 1.0]], [[2.0, 2.0], [3.0, 3.0]]],
    }


def test_wkt_polygon_with_hole_and_multipolygon():
    poly = _wkt_to_geojson("POLYGON((0 0, 4 0, 4 4, 0 0),(1 1, 2 1, 2 2, 1 1))")
    assert poly["type"] == "Polygon" and len(poly["coordinates"]) == 2  # exterior + hole
    assert poly["coordinates"][0][0] == [0.0, 0.0]
    multi = _wkt_to_geojson("MULTIPOLYGON(((0 0,1 0,1 1,0 0)),((2 2,3 2,3 3,2 2)))")
    assert multi["type"] == "MultiPolygon" and len(multi["coordinates"]) == 2


def test_wkt_rejects_empty_and_unsupported():
    assert _wkt_to_geojson("") is None
    assert _wkt_to_geojson(None) is None
    assert _wkt_to_geojson("POLYGON EMPTY") is None
    assert _wkt_to_geojson("GEOMETRYCOLLECTION(POINT(0 0))") is None  # not a kind we emit


# value sniffers (undocumented headers picked by value shape)


def test_sniffers_pick_expected_columns():
    assert registers.poly_col({"a": "text", "g": "MULTIPOLYGON(((0 0,1 0,1 1,0 0)))"}) == "g"
    assert registers.line_col({"a": "x", "g": "LINESTRING(0 0,1 1)"}) == "g"
    assert registers.point_col({"a": "x", "position": "POINT(1 2)"}) == "position"
    assert registers.ejerlav_kode_col({"id_lokalId": "e", "landsejerlavskode": "60851"}) == (
        "landsejerlavskode"
    )
    assert registers.poly_col({"a": "not geometry"}) is None


def test_sniff_state_caches_resolution():
    spec = _spec("dar_navngivenvej")  # geometry header sniffed via line_col
    st = SniffState(spec)
    geo_idx = next(i for i, c in enumerate(spec.columns) if c.name == "geometry")
    assert st.resolve(geo_idx, {"myline": "LINESTRING(0 0,1 1)"}) == "myline"
    # a later row with a different candidate keeps the first resolution (headers stable per file)
    assert st.resolve(geo_idx, {"otherline": "LINESTRING(2 2,3 3)"}) == "myline"


# shape_row: Kind conversions


def test_shape_row_text_strips_and_nulls_empty():
    out = _shape(_spec("dar_postnummer"), {"id_lokalId": "p1", "postnr": " 4850 ", "navn": ""})
    assert out == {"id": "p1", "postnr": "4850", "navn": None}


def test_shape_row_geojson_is_compact_string():
    spec = _spec("dar_navngivenvej")
    out = _shape(spec, {"id_lokalId": "v1", "vejnavn": "Hovedgaden", "line": "LINESTRING(0 0,1 1)"})
    assert out["id"] == "v1" and out["vejnavn"] == "Hovedgaden"
    assert out["geometry"] == '{"type":"LineString","coordinates":[[0.0,0.0],[1.0,1.0]]}'


def test_shape_row_point_xy_splits_into_doubles():
    spec = _spec("dar_adressepunkt")
    out = _shape(spec, {"id_lokalId": "ap1", "position": "POINT(722345.67 6179535.68)"})
    assert out == {"id": "ap1", "x": 722345.67, "y": 6179535.68}
    # no wkt point in the row -> both ordinate columns null, shape still stable
    assert _shape(spec, {"id_lokalId": "ap2", "position": ""}) == {
        "id": "ap2",
        "x": None,
        "y": None,
    }


def test_shape_row_point_text_verbatim():
    spec = _spec("mat_centroide")
    out = _shape(spec, {"jordstykkeLokalId": "j1", "geometri": "POINT (539385.682 6350372.987)"})
    assert out == {"jordstykke": "j1", "centroid": "539385.682 6350372.987"}  # no float reformat


def test_shape_row_double_kind():
    spec = EntitySpec(
        "X",
        "E",
        "x_e",
        (Column("id", "id_lokalId"), Column("val", "v", Kind.DOUBLE)),
        Currency.OPEN,
    )
    assert _shape(spec, {"id_lokalId": "e1", "v": "3.5"}) == {"id": "e1", "val": 3.5}
    assert _shape(spec, {"id_lokalId": "e2", "v": ""}) == {"id": "e2", "val": None}


# shape_row: keep-list projection + canonical rename + dialect


def test_shape_row_projects_keep_list_and_renames_to_snake_case():
    spec = _spec("dar_husnummer")
    row = {
        "id_lokalId": "h1",
        "husnummertekst": "41A",
        "navngivenVej": "v1",
        "postnummer": "p1",
        "supplerendeBynavn": "s1",
        "adgangspunkt": "ap1",
        "vejpunkt": "vp1",
        "kommuneinddeling": "k1",
        "sogneinddeling": "sg1",
        "jordstykke": "j1",
        "status": "3",  # now a staged lifecycle column (classified in snapshot sql)
        "virkningFra": "2020-01-01T00:00:00Z",
        "forretningshaendelse": "noise",  # off keep-list -> dropped
    }
    assert _shape(spec, row) == {
        "id": "h1",
        "husnummertekst": "41A",
        "navngivenvej": "v1",
        "postnummer": "p1",
        "supplerende_bynavn": "s1",
        "adgangspunkt": "ap1",
        "vejpunkt": "vp1",
        "kommuneinddeling": "k1",
        "sogneinddeling": "sg1",
        "jordstykke": "j1",
        "status": "3",
        "virkningfra": "2020-01-01T00:00:00+00:00",  # canonicalized to utc iso
        "virkningtil": None,
    }


def test_shape_row_door_dialect_either_spelling():
    spec = _spec("dar_adresse")
    ascii_row = {
        "id_lokalId": "a1",
        "husnummer": "h1",
        "etagebetegnelse": "2",
        "doerbetegnelse": "tv",
    }
    oe_row = {"id_lokalId": "a2", "husnummer": "h2", "etagebetegnelse": "1", "dørbetegnelse": "th"}
    assert _shape(spec, ascii_row)["door"] == "tv"
    assert _shape(spec, oe_row)["door"] == "th"  # æ/ø bitemporal spelling absorbed by the src tuple


def test_shape_row_version_mix_tolerance():
    # a delta run can mix data-model versions; each file gets its own SniffState + dialect, so a
    # V3-spelled (dør) and a V4-spelled (doer) row shape to the same canonical staging row.
    spec = _spec("dar_adresse")
    v4 = shape_row(
        {"id_lokalId": "a1", "husnummer": "h1", "etagebetegnelse": "2", "doerbetegnelse": "tv"},
        spec,
        SniffState(spec),
    )
    v3 = shape_row(
        {"id_lokalId": "a2", "husnummer": "h2", "etagebetegnelse": "1", "dørbetegnelse": "th"},
        spec,
        SniffState(spec),
    )
    # lifecycle columns absent in the rows -> shaped null, staging shape stays stable
    assert v4 == {
        "id": "a1",
        "husnummer": "h1",
        "etage": "2",
        "door": "tv",
        "status": None,
        "virkningfra": None,
        "virkningtil": None,
    }
    assert v3 == {
        "id": "a2",
        "husnummer": "h2",
        "etage": "1",
        "door": "th",
        "status": None,
        "virkningfra": None,
        "virkningtil": None,
    }


def test_ds_stednavn_shapes_objectid_pk_without_id_lokalid():
    # ds carries no id_lokalId; the identity is objectid and skrivemaade is the name; aktualitet
    # is now staged (historisk skrivemåder are served as retired rows, not filtered out)
    spec = _spec("ds_stednavn")
    row = {
        "objectid": "n1",
        "skrivemaade": "Furesø",
        "navngivetSted_objectid": "p1",
        "aktualitet": "iAnvendelse",
    }
    assert _shape(spec, row) == {
        "objectid": "n1",
        "skrivemaade": "Furesø",
        "navngivetsted_objectid": "p1",
        "aktualitet": "iAnvendelse",
    }


def test_pk_value_reads_source_pk_cleaned():
    assert pk_value({"id_lokalId": " h1 "}, _spec("dar_husnummer")) == "h1"
    assert pk_value({"objectid": "n1"}, _spec("ds_stednavn")) == "n1"
    assert pk_value({"jordstykkeLokalId": "j1"}, _spec("mat_centroide")) == "j1"
    assert pk_value({}, _spec("dar_husnummer")) is None
