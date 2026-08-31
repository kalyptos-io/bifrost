"""stednavne join port (no db): the ds geometry-entity catalog + the name<->geometry join sql.
mirrors the retired registry.STEDNAVNE_GEOM_ENTITIES + _stednavn_records.
"""

from __future__ import annotations

from bifrost_sync.snapshot.stednavne import _DS_GEOMS, _STEDNAVN_SQL

_ABSENT = {"ds_rute", "ds_faergerutelinje", "ds_faergerutepunkt", "ds_ubearbejdetnavnlinje"}


def test_ds_geoms_catalog_covers_30_typed_entities():
    tables = {t for t, _ in _DS_GEOMS}
    assert len(_DS_GEOMS) == 30  # every geometry-carrying ds entity, ds_stednavn (names) excluded
    assert "ds_stednavn" not in tables
    assert tables >= _ABSENT  # the 4 absent-table names are in the catalog (tolerated at runtime)
    assert dict(_DS_GEOMS)["ds_bebyggelse"] == "bebyggelse"  # each tags its places with a wire type


def test_stednavn_sql_joins_names_to_geometry_on_objectid():
    sql = _STEDNAVN_SQL.format(staging="datafordeler", table="ds_bebyggelse")
    assert '"datafordeler".ds_stednavn s' in sql
    assert '"datafordeler".ds_bebyggelse g' in sql
    assert "g.objectid = s.navngivetsted_objectid" in sql  # name ref -> place objectid
    assert "s._deleted IS NOT TRUE" in sql and "g._deleted IS NOT TRUE" in sql
