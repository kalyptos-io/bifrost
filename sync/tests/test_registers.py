"""catalog invariants: entity counts, unique tables, resolvable pks, per-register knobs."""

from __future__ import annotations

from collections import Counter

from bifrost_sync.pipeline import _SOURCE_NAME, hints
from bifrost_sync.registers import ALL_ENTITIES, DAGI, DAR, DS, EBR, MAT, Currency


def test_all_entities_count():
    # 6 dar + 2 dar hist + 7 dagi + 8 mat + 1 ds name + 30 ds geometry + 1 ebr
    assert len(ALL_ENTITIES) == 55


def test_register_counts():
    assert Counter(s.register for s in ALL_ENTITIES) == {DAR: 8, DAGI: 7, MAT: 8, DS: 31, EBR: 1}


def test_every_register_has_a_source_name():
    # a missing entry KeyErrors _source() on every staging pass
    assert {s.register for s in ALL_ENTITIES} <= _SOURCE_NAME.keys()


def test_hints_build_for_every_spec():
    # a Kind missing from _KIND_TYPE KeyErrors staging on every reconcile
    for s in ALL_ENTITIES:
        assert hints(s)


def test_tables_are_unique():
    tables = [s.table for s in ALL_ENTITIES]
    assert len(tables) == len(set(tables))


def test_pk_out_resolves_for_every_spec():
    for s in ALL_ENTITIES:
        assert isinstance(s.pk_out, str) and s.pk_out
        assert any(c.src == s.pk for c in s.columns)  # the pk source is a kept column


def test_retention_is_28_for_dagi_else_14():
    for s in ALL_ENTITIES:
        assert s.delta_retention_days == (28 if s.register == DAGI else 14)


def test_only_original_mat_tables_split_totals_per_municipality():
    # the 3 bfe-property mat entities + ebr ship national current csvs, so they never muni-split
    split = {s.table for s in ALL_ENTITIES if s.muni_split_totals}
    assert split == {
        "mat_samletfastejendom",
        "mat_jordstykke",
        "mat_ejerlav",
        "mat_centroide",
        "mat_lodflade",
    }


def test_ejerlejlighed_keeps_truncated_bpfg_parent_headers():
    s = next(s for s in ALL_ENTITIES if s.table == "mat_ejerlejlighed")
    srcs = {c.src for c in s.columns}
    assert "b_BygningPaaFremmedGrundPunktL" in srcs
    assert "b_BygningPaaFremmedGrundFladeL" in srcs


def test_ebr_spec_columns_and_currency():
    s = next(s for s in ALL_ENTITIES if s.register == EBR)
    assert s.table == "ebr_ejendomsbeliggenhed"
    assert s.currency is Currency.MAT
    cols = {c.name: c.src for c in s.columns}
    assert cols["bfe"] == "bestemtFastEjendomBFENr"
    assert cols["adresse_lokalid"] == "adresseLokalId"
    assert "betegnelse" not in cols and "husnummer_lokalid" not in cols


def test_adressepunkt_currency_is_open():
    # adressepunkt status (8/9) is redundant with the husnummer lifecycle; kept as geometry only
    s = next(s for s in ALL_ENTITIES if s.table == "dar_adressepunkt")
    assert s.currency is Currency.OPEN
    assert not any(c.name == "status" for c in s.columns)  # no status column staged


def test_hist_specs_composite_key_and_bitemporal_baseline():
    hist = [s for s in ALL_ENTITIES if s.is_hist]
    assert {s.table for s in hist} == {"dar_navngivenvej_hist", "dar_postnummer_hist"}
    for s in hist:
        assert s.version_key == ("registreringfra", "virkningfra")
        assert s.merge_key == ["id", "registreringfra", "virkningfra"]
        assert s.baseline_variant == "bitemporal"
        assert (
            s.download_name == f"{s.entity}_hist"
        )  # distinct download basename from the main spec
        names = {c.name for c in s.columns}
        assert {
            "registreringfra",
            "registreringtil",
            "virkningfra",
            "virkningtil",
            "status",
        } <= names


def test_lifecycle_columns_staged_on_the_classified_mains():
    for table in (
        "dar_navngivenvej",
        "dar_husnummer",
        "dar_adresse",
        "mat_jordstykke",
        "mat_samletfastejendom",
        "mat_ejerlejlighed",
    ):
        s = next(s for s in ALL_ENTITIES if s.table == table)
        assert {"status", "virkningfra", "virkningtil"} <= {c.name for c in s.columns}


def test_ds_stednavn_baseline_is_bitemporal_and_keeps_aktualitet():
    s = next(s for s in ALL_ENTITIES if s.table == "ds_stednavn")
    assert s.baseline_variant == "bitemporal"
    assert any(c.name == "aktualitet" for c in s.columns)


def test_main_specs_baseline_current_except_ds_stednavn():
    for s in ALL_ENTITIES:
        if s.is_hist or s.table == "ds_stednavn":
            continue
        assert s.baseline_variant == "current"


def test_ds_stednavn_is_aktualitet_keyed_on_objectid():
    s = next(s for s in ALL_ENTITIES if s.table == "ds_stednavn")
    assert s.currency is Currency.AKTUALITET
    assert s.pk == "objectid" and s.pk_out == "objectid"


def test_ds_geometry_entities_carry_type_labels():
    geom = [s for s in ALL_ENTITIES if s.register == DS and s.table != "ds_stednavn"]
    assert len(geom) == 30
    assert all(s.type_label and s.pk == "objectid" and s.currency is Currency.OPEN for s in geom)


def test_centroide_keyed_on_parcel_not_own_id():
    s = next(s for s in ALL_ENTITIES if s.table == "mat_centroide")
    assert s.pk == "jordstykkeLokalId" and s.pk_out == "jordstykke"


def test_mat_jordstykke_carries_kommunekode_directly():
    # national deltas have no file context: kommune comes from the kommuneLokalId column
    s = next(s for s in ALL_ENTITIES if s.table == "mat_jordstykke")
    assert any(c.name == "kommunekode" and c.src == "kommuneLokalId" for c in s.columns)
