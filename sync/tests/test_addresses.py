"""address SQL builder + record derivation ports (no db): schema qualification of the join, and the
street/husnr guards + husnr split iter_addresses applies per row.
"""

from __future__ import annotations

from bifrost_sync.snapshot.addresses import (
    _row_to_record,
    address_sql,
    ensure_hist_indexes,
    hist_index_sql,
)

_ROW = {
    "id": "a1",
    "street_name": "Tejnvej",
    "husnummertekst": "116H",
    "sub_locality": " Hesnæs ",
    "floor": "2",
    "door": "tv",
    "postcode": "3770",
    "city": "Allinge",
    "adgangspunkt_x": 869826.85,
    "adgangspunkt_y": 6138379.16,
    "vejpunkt_x": None,
    "vejpunkt_y": None,
    "kommunekode": "0400",
    "regionskode": "1084",
    "sognekode": "7559",
    "retskredsnummer": "16",
    "politikredsnummer": None,
    "opstillingskredsnummer": None,
    "jordstykke": None,
    "ejendom_bfe": "100400001",
    "lifecycle": "current",
}


def test_address_sql_qualifies_staging_and_gen_schema():
    sql = address_sql("datafordeler", "gen_x")
    assert '"datafordeler".dar_adresse a' in sql
    assert '"datafordeler".dar_husnummer h' in sql
    assert '"gen_x"._district_stamp ds' in sql  # PIP stamp joined from the gen schema
    assert '"gen_x".matrikel m' in sql  # jordstykke currency gate
    assert "k.region_lokalid" in sql  # region chained off the kommune row
    assert "a._deleted IS NOT TRUE" in sql  # defensive dlt tombstone filter


def test_address_sql_stamps_ejendom_bfe_via_ebr_unit_join():
    sql = address_sql("datafordeler", "gen_x")
    assert '"datafordeler".ebr_ejendomsbeliggenhed eb' in sql
    assert '"gen_x".ejendom u' in sql
    assert "u.type = 'ejerlejlighed'" in sql  # membership+type validated before dedup
    assert "COALESCE(unit.bfe, m.bfe) AS ejendom_bfe" in sql  # unit stamp else ground sfe


def test_row_to_record_splits_husnr_and_carries_fields():
    rec = _row_to_record(_ROW)
    assert rec is not None
    assert rec["house_number"] == "116" and rec["house_letter"] == "H"
    assert rec["sub_locality"] == "Hesnæs"  # cleaned
    assert rec["adgangspunkt_x"] == 869826.85 and rec["vejpunkt_x"] is None
    assert rec["kommunekode"] == "0400" and rec["retskredsnummer"] == "16"
    assert rec["ejendom_bfe"] == "100400001"
    assert rec["lifecycle"] == "current"
    assert set(rec) >= {"city", "lifecycle", "jordstykke", "opstillingskredsnummer"}


def test_address_sql_point_in_time_joins_gated_on_hist_tables():
    plain = address_sql("datafordeler", "gen_x")
    assert "dar_navngivenvej_hist" not in plain  # no hist tables -> no lateral, current names only
    withhist = address_sql("datafordeler", "gen_x", nv_hist=True, pn_hist=True)
    assert "dar_navngivenvej_hist nvh" in withhist  # point-in-time street name lateral
    assert "dar_postnummer_hist pnh" in withhist  # point-in-time city name lateral
    assert "sub.lifecycle = ANY($1)" in withhist  # retired/abandoned pick the point-in-time name


def test_row_to_record_drops_no_street_and_unsplittable_husnr():
    assert _row_to_record({**_ROW, "street_name": None}) is None
    assert _row_to_record({**_ROW, "husnummertekst": ""}) is None


def test_row_to_record_drops_missing_postcode():
    assert _row_to_record({**_ROW, "postcode": None}) is None
    assert _row_to_record({**_ROW, "postcode": " "}) is None


def test_hist_index_sql_targets_both_hist_tables_schema_qualified():
    sql = hist_index_sql("datafordeler")
    joined = " ".join(sql)
    assert '"datafordeler".dar_navngivenvej_hist (id)' in joined  # id btree the pit lateral probes
    assert '"datafordeler".dar_postnummer_hist (id)' in joined
    assert joined.count("CREATE INDEX IF NOT EXISTS") == 2  # idempotent, survives dlt refresh


class _StubConn:
    def __init__(self, present: set[str]):
        self.present = present
        self.executed: list[str] = []

    async def fetchval(self, _sql: str, qualified: str) -> str | None:
        return qualified if qualified in self.present else None

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)


async def test_ensure_hist_indexes_skips_missing_creates_present():
    conn = _StubConn({"datafordeler.dar_navngivenvej_hist"})
    await ensure_hist_indexes(conn, staging="datafordeler")
    assert len(conn.executed) == 1  # only the present table's index is created
    assert "dar_navngivenvej_hist_id_idx" in conn.executed[0]
    assert "dar_postnummer_hist" not in " ".join(conn.executed)
