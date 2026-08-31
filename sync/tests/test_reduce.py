"""the three fold shapes: versioned (registration-open/virkning-latest, tombstone-on-none),
aktualitet (latest-gen, never tombstone), and history pass-through (composite-key, no hard-delete).

the load-bearing semantic: extract classifies, never filters - every lifecycle state stages; the
fold only decides which VERSION of an id is current-effective, not which lifecycle is served.
"""

from __future__ import annotations

import csv
import io
import zipfile

import pytest
from bifrost_sync.reduce import baseline_rows, reduce_delta_files
from bifrost_sync.registers import ALL_ENTITIES, EntitySpec

# raw bitemporal metadata the fold reads (never staged on a main table). blank registreringTil = the
# registration is open (a candidate); a set one is a superseded correction, excluded.
_OPEN = {"registreringTil": "", "registreringFra": "2020-01-01T00:00:00Z", "virkningTil": ""}
_CLOSED = {
    "registreringTil": "2026-07-09T00:00:00Z",
    "registreringFra": "2019-01-01T00:00:00Z",
    "virkningTil": "",
}


def _spec(table: str) -> EntitySpec:
    return next(s for s in ALL_ENTITIES if s.table == table)


def _zip(path, rows: list[dict], *, delim: str = ",") -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), delimiter=delim)
    w.writeheader()
    w.writerows(rows)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("data.csv", buf.getvalue().encode("utf-8"))
    return str(path)


def _by_pk(rows: list[dict], spec: EntitySpec) -> dict[str, dict]:
    return {r[spec.pk_out]: r for r in rows}


def _postnr(id_: str, navn: str, meta: dict, *, vfra: str = "2020-01-01T00:00:00Z") -> dict:
    return {"id_lokalId": id_, "postnr": "4850", "navn": navn, "virkningFra": vfra, **meta}


# versioned fold: registration-open + virkning-latest, tombstone only on zero open rows


def test_close_plus_open_upserts_open_payload_open_first(tmp_path):
    spec = _spec("dar_postnummer")
    z = _zip(tmp_path / "g.zip", [_postnr("p1", "New", _OPEN), _postnr("p1", "Old", _CLOSED)])
    out = list(reduce_delta_files([(1, z)], spec))
    assert out == [{"id": "p1", "postnr": "4850", "navn": "New"}]


def test_close_plus_open_upserts_open_payload_close_first(tmp_path):
    # a registration-superseded close row must never shadow the registration-open row
    spec = _spec("dar_postnummer")
    z = _zip(tmp_path / "g.zip", [_postnr("p1", "Old", _CLOSED), _postnr("p1", "New", _OPEN)])
    out = list(reduce_delta_files([(1, z)], spec))
    assert out == [{"id": "p1", "postnr": "4850", "navn": "New"}]


def test_close_only_tombstones(tmp_path):
    spec = _spec("dar_postnummer")
    z = _zip(tmp_path / "g.zip", [_postnr("p2", "Gone", _CLOSED)])
    assert list(reduce_delta_files([(1, z)], spec)) == [{"id": "p2", "_deleted": True}]


def test_open_non_gaeldende_row_is_kept_not_dropped(tmp_path):
    # classify-don't-filter: a foreløbig/nedlagt row (registration-open) stages; snapshot classifies
    spec = _spec("dar_husnummer")  # carries a staged status column
    row = {
        "id_lokalId": "h1",
        "husnummertekst": "5",
        "status": "2",  # foreløbig
        **_OPEN,
        "virkningFra": "2020-01-01T00:00:00Z",
    }
    out = list(reduce_delta_files([(1, _zip(tmp_path / "g.zip", [row]))], spec))
    assert out[0]["id"] == "h1" and out[0]["status"] == "2" and "_deleted" not in out[0]


def test_current_effective_version_wins_over_future(tmp_path):
    # a registered future-effective version must not hide the currently-effective one (the lifecycle
    # CASE would classify it preliminary -> dropped from default current-only serving)
    spec = _spec("dar_postnummer")
    z = _zip(
        tmp_path / "g.zip",
        [
            _postnr("p1", "Now", _OPEN, vfra="2020-01-01T00:00:00Z"),
            _postnr("p1", "Future", _OPEN, vfra="2099-01-01T00:00:00Z"),
        ],
    )
    assert list(reduce_delta_files([(1, z)], spec)) == [
        {"id": "p1", "postnr": "4850", "navn": "Now"}
    ]


def test_future_only_version_still_folds(tmp_path):
    # no currently-effective version -> the future one wins (served preliminary, not dropped)
    spec = _spec("dar_postnummer")
    z = _zip(tmp_path / "g.zip", [_postnr("p1", "Future", _OPEN, vfra="2099-01-01T00:00:00Z")])
    assert list(reduce_delta_files([(1, z)], spec)) == [
        {"id": "p1", "postnr": "4850", "navn": "Future"}
    ]


def test_two_current_effective_versions_pick_virkning_latest(tmp_path):
    # both effective now (open virkning window) -> the later virkningFra breaks the tie
    spec = _spec("dar_postnummer")
    z = _zip(
        tmp_path / "g.zip",
        [
            _postnr("p1", "Older", _OPEN, vfra="2020-01-01T00:00:00Z"),
            _postnr("p1", "Newer", _OPEN, vfra="2024-01-01T00:00:00Z"),
        ],
    )
    assert list(reduce_delta_files([(1, z)], spec)) == [
        {"id": "p1", "postnr": "4850", "navn": "Newer"}
    ]


def test_upsert_gen1_then_tombstone_gen3(tmp_path):
    spec = _spec("dar_postnummer")
    g1 = _zip(tmp_path / "g1.zip", [_postnr("p1", "Live", _OPEN)])
    g3 = _zip(tmp_path / "g3.zip", [_postnr("p1", "Gone", _CLOSED)])
    assert list(reduce_delta_files([(1, g1), (3, g3)], spec)) == [{"id": "p1", "_deleted": True}]


def test_tombstone_gen1_then_upsert_gen3(tmp_path):
    spec = _spec("dar_postnummer")
    g1 = _zip(tmp_path / "g1.zip", [_postnr("p1", "Gone", _CLOSED)])
    g3 = _zip(tmp_path / "g3.zip", [_postnr("p1", "Back", _OPEN)])
    assert list(reduce_delta_files([(1, g1), (3, g3)], spec)) == [
        {"id": "p1", "postnr": "4850", "navn": "Back"}
    ]


def test_unsorted_files_defensive_later_gen_still_wins(tmp_path):
    spec = _spec("dar_postnummer")
    g1 = _zip(tmp_path / "g1.zip", [_postnr("p1", "Old", _OPEN)])
    g3 = _zip(tmp_path / "g3.zip", [_postnr("p1", "Gone", _CLOSED)])
    assert list(reduce_delta_files([(3, g3), (1, g1)], spec)) == [{"id": "p1", "_deleted": True}]


def test_delta_missing_registreringtil_aborts(tmp_path):
    # registreringTil dropped: the fold would read every close-row as open -> abort loudly
    spec = _spec("dar_postnummer")
    row = {"id_lokalId": "p1", "postnr": "4850", "navn": "X", "virkningFra": "2020-01-01"}
    with pytest.raises(SystemExit):
        list(reduce_delta_files([(1, _zip(tmp_path / "g.zip", [row]))], spec))


def test_dagi_delta_missing_virkningtil_aborts(tmp_path):
    # dagi lifecycle is the virkning window: dropping virkningTil would fail open to current
    spec = _spec("dagi_kommuneinddeling")
    row = {"id_lokalId": "k1", "kommunekode": "0101", "registreringTil": "", "virkningFra": ""}
    with pytest.raises(SystemExit):
        list(reduce_delta_files([(1, _zip(tmp_path / "g.zip", [row]))], spec))


def test_version_mixed_files_fold_through_one_spec(tmp_path):
    # a V4-spelled (doer) file and a bitemporal-spelled (dør) file share the dar_adresse spec
    spec = _spec("dar_adresse")
    v4 = _zip(
        tmp_path / "v4.zip",
        [
            {
                "id_lokalId": "a1",
                "husnummer": "h1",
                "etagebetegnelse": "2",
                "doerbetegnelse": "tv",
                "status": "3",
                **_OPEN,
            }
        ],
    )
    v3 = _zip(
        tmp_path / "v3.zip",
        [
            {
                "id_lokalId": "a2",
                "husnummer": "h2",
                "etagebetegnelse": "1",
                "dørbetegnelse": "th",
                "status": "3",
                **_OPEN,
            }
        ],
    )
    by_pk = _by_pk(list(reduce_delta_files([(1, v4), (2, v3)], spec)), spec)
    assert (
        by_pk["a1"]["door"] == "tv" and by_pk["a1"]["etage"] == "2" and by_pk["a1"]["status"] == "3"
    )
    assert by_pk["a2"]["door"] == "th" and by_pk["a2"]["etage"] == "1"


# aktualitet fold (ds stednavn): keeps historic skrivemåder, never tombstones


def _sted(objectid: str, name: str, aktualitet: str) -> dict:
    return {
        "objectid": objectid,
        "skrivemaade": name,
        "navngivetSted_objectid": "s1",
        "aktualitet": aktualitet,
    }


def test_aktualitet_keeps_historic_and_never_tombstones(tmp_path):
    spec = _spec("ds_stednavn")
    z = _zip(
        tmp_path / "g.zip",
        [_sted("A", "Furesø", "iAnvendelse"), _sted("B", "Gammel", "historisk")],
    )
    by_pk = _by_pk(list(reduce_delta_files([(1, z)], spec)), spec)
    assert by_pk["A"] == {
        "objectid": "A",
        "skrivemaade": "Furesø",
        "navngivetsted_objectid": "s1",
        "aktualitet": "iAnvendelse",
    }
    assert by_pk["B"]["skrivemaade"] == "Gammel"  # historic skrivemåde survives (served as retired)
    assert by_pk["B"]["aktualitet"] == "historisk"
    assert all("_deleted" not in r for r in by_pk.values())


def test_aktualitet_latest_generation_wins(tmp_path):
    spec = _spec("ds_stednavn")
    g1 = _zip(tmp_path / "g1.zip", [_sted("A", "Old", "iAnvendelse")])
    g2 = _zip(tmp_path / "g2.zip", [_sted("A", "New", "iAnvendelse")])
    out = list(reduce_delta_files([(1, g1), (2, g2)], spec))
    assert out == [
        {
            "objectid": "A",
            "skrivemaade": "New",
            "navngivetsted_objectid": "s1",
            "aktualitet": "iAnvendelse",
        }
    ]


def test_aktualitet_current_version_wins_within_generation(tmp_path):
    # one objectid with both a historisk and an iAnvendelse version folds to the current one,
    # deterministically (independent of row order) - dlt last-write-wins would flip on order
    spec = _spec("ds_stednavn")
    z = _zip(tmp_path / "g.zip", [_sted("A", "Old", "historisk"), _sted("A", "New", "iAnvendelse")])
    out = list(reduce_delta_files([(1, z)], spec))
    assert out == [
        {
            "objectid": "A",
            "skrivemaade": "New",
            "navngivetsted_objectid": "s1",
            "aktualitet": "iAnvendelse",
        }
    ]


# history fold (*_hist): composite-key pass-through, in-place close, no hard-delete


def _nvh(
    id_: str, vejnavn: str, rfra: str, vfra: str, *, rtil: str = "", status: str = "3"
) -> dict:
    return {
        "id_lokalId": id_,
        "vejnavn": vejnavn,
        "status": status,
        "virkningFra": vfra,
        "virkningTil": "",
        "registreringFra": rfra,
        "registreringTil": rtil,
    }


def test_hist_keeps_all_versions_on_composite_key(tmp_path):
    spec = _spec("dar_navngivenvej_hist")
    z = _zip(
        tmp_path / "g.zip",
        [
            _nvh("v1", "Gamle Vej", "2010-01-01T00:00:00Z", "2010-01-01T00:00:00Z"),
            _nvh("v1", "Nye Vej", "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"),
        ],
    )
    out = list(reduce_delta_files([(1, z)], spec))
    assert {r["vejnavn"] for r in out} == {"Gamle Vej", "Nye Vej"}  # both versions retained
    assert all("_deleted" not in r for r in out)


def test_hist_closing_delta_updates_same_key_in_place(tmp_path):
    # a later generation carrying the same (id, registreringfra, virkningfra) with registreringtil
    # set overrides the open row -> one row, now registration-closed
    spec = _spec("dar_navngivenvej_hist")
    g1 = _zip(
        tmp_path / "g1.zip", [_nvh("v1", "Vej", "2010-01-01T00:00:00Z", "2010-01-01T00:00:00Z")]
    )
    g2 = _zip(
        tmp_path / "g2.zip",
        [
            _nvh(
                "v1",
                "Vej",
                "2010-01-01T00:00:00Z",
                "2010-01-01T00:00:00Z",
                rtil="2020-01-01T00:00:00Z",
            )
        ],
    )
    out = list(reduce_delta_files([(1, g1), (2, g2)], spec))
    assert out == [
        {
            "id": "v1",
            "vejnavn": "Vej",
            "status": "3",
            "virkningfra": "2010-01-01T00:00:00+00:00",  # canonicalized to utc iso
            "virkningtil": None,
            "registreringfra": "2010-01-01T00:00:00+00:00",
            "registreringtil": "2020-01-01T00:00:00+00:00",
        }
    ]


def test_hist_null_temporal_key_skips_row(tmp_path, capsys):
    # a null composite-key column silently never merges -> skip the row + warn, don't sink the run
    spec = _spec("dar_navngivenvej_hist")
    good = _nvh("v1", "Vej", "2010-01-01T00:00:00Z", "2010-01-01T00:00:00Z")
    bad = {**good, "vejnavn": "Bad", "virkningFra": ""}  # null virkningfra -> unmergeable key
    out = list(reduce_delta_files([(1, _zip(tmp_path / "g.zip", [good, bad]))], spec))
    assert [r["vejnavn"] for r in out] == ["Vej"]  # only the well-keyed row survives
    assert "null composite key" in capsys.readouterr().out


# baseline_rows: stream + shape all (classify, never filter)


def test_baseline_streams_every_lifecycle_state(tmp_path):
    # a nedlagt (status 4) row in a current total is NOT dropped - it stages, classified later
    spec = _spec("dar_husnummer")
    z = _zip(
        tmp_path / "t.zip",
        [
            {"id_lokalId": "h1", "husnummertekst": "1", "status": "3"},
            {"id_lokalId": "h2", "husnummertekst": "2", "status": "4"},  # nedlagt: kept
        ],
    )
    out = _by_pk(list(baseline_rows([z], spec)), spec)
    assert set(out) == {"h1", "h2"}
    assert out["h2"]["status"] == "4"
    assert all("_deleted" not in r for r in out.values())


def test_baseline_muni_split_streams_all_zips(tmp_path):
    spec = _spec("mat_samletfastejendom")
    z1 = _zip(tmp_path / "0101.zip", [{"id_lokalId": "e1", "BFEnummer": "1", "status": "Gældende"}])
    z2 = _zip(
        tmp_path / "0201.zip", [{"id_lokalId": "e2", "BFEnummer": "2", "status": "Historisk"}]
    )
    out = _by_pk(list(baseline_rows([z1, z2], spec)), spec)
    assert out.keys() == {"e1", "e2"}
    assert out["e2"]["status"] == "Historisk"  # a historisk sfe stages (classified retired later)


def test_baseline_ds_stednavn_keeps_historic(tmp_path):
    # ds baseline is bitemporal; historic skrivemåder (distinct objectids) stage as rows
    spec = _spec("ds_stednavn")
    z = _zip(
        tmp_path / "ds.zip",
        [_sted("A", "Furesø", "iAnvendelse"), _sted("B", "Gammel", "historisk")],
    )
    out = _by_pk(list(baseline_rows([z], spec)), spec)
    assert out.keys() == {"A", "B"}


def test_baseline_ds_stednavn_folds_multi_version_objectid_to_current(tmp_path):
    # an objectid with both versions folds to iAnvendelse (deterministic, not dlt last-write-wins);
    # a historisk-only objectid is still kept and staged (served as retired)
    spec = _spec("ds_stednavn")
    z = _zip(
        tmp_path / "ds.zip",
        [
            _sted("A", "Old", "historisk"),
            _sted("A", "Furesø", "iAnvendelse"),
            _sted("B", "Gammel", "historisk"),
        ],
    )
    out = _by_pk(list(baseline_rows([z], spec)), spec)
    assert out["A"]["skrivemaade"] == "Furesø" and out["A"]["aktualitet"] == "iAnvendelse"
    assert out["B"]["aktualitet"] == "historisk"


def test_baseline_hist_streams_versions_and_skips_null_keys(tmp_path, capsys):
    spec = _spec("dar_navngivenvej_hist")
    ok = _zip(
        tmp_path / "ok.zip",
        [
            _nvh("v1", "A", "2010-01-01T00:00:00Z", "2010-01-01T00:00:00Z"),
            _nvh("v1", "B", "2020-01-01T00:00:00Z", "2020-01-01T00:00:00Z"),
        ],
    )
    assert len(list(baseline_rows([ok], spec))) == 2
    bad = _zip(
        tmp_path / "bad.zip",
        [
            {
                "id_lokalId": "v1",
                "vejnavn": "A",
                "status": "3",
                "virkningFra": "",  # null composite-key column -> skipped, not aborted
                "virkningTil": "",
                "registreringFra": "2010-01-01",
                "registreringTil": "",
            }
        ],
    )
    assert list(baseline_rows([bad], spec)) == []
    assert "null composite key" in capsys.readouterr().out
