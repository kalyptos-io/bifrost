"""the address join: dar_adresse -> dar_husnummer -> its dims (navngivenvej/postnummer/
supplerende_bynavn/adressepunkt x2) -> dagi kommune/sogn codes via husnummer refs, region chained
off the kommune row's region_lokalid -> the PIP _district_stamp -> gen.matrikel currency gate on
jordstykke -> the ejendom_bfe stamp (ebr ejerlejlighed at the address else the ground sfe, via
gen.ejendom). iter_addresses re-derives the exact per-address corpus record dicts from the stream.

lifecycle is classified from the adresse status (the authoritative address lifecycle); a retired
address renders point-in-time street/city names via LEFT JOIN LATERAL on the *_hist tables (the name
version whose virkning window contains the address's retirement instant), COALESCEd with the current
dim name so a current address is unaffected. husnummer inner-joins (a tombstoned ref drops the
adresse silently), the dims left-join, street+husnr+postcode guards drop the row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg

from ..extract import clean
from . import STAGING
from .lifecycle import ABANDONED, RETIRED, dar_case
from .read import has_table, stream
from .records import split_husnr

_HIST_LIFECYCLES = (RETIRED, ABANDONED)  # only these render a point-in-time name

# point-in-time name: the registration-current hist version whose virkning window contains the
# address's retirement instant (its own virkningfra). virkning columns stage as canonical utc iso
# (extract Kind.TIMESTAMP), so this text comparison is chronological across both tables.
_PIT_STREET = """
    LEFT JOIN LATERAL (
        SELECT nvh.vejnavn FROM "{staging}".dar_navngivenvej_hist nvh
        WHERE nvh.id = h.navngivenvej AND nvh.registreringtil IS NULL
          AND (nvh.virkningfra IS NULL OR nvh.virkningfra <= a.virkningfra)
          AND (nvh.virkningtil IS NULL OR nvh.virkningtil > a.virkningfra)
        ORDER BY nvh.virkningfra DESC NULLS LAST LIMIT 1
    ) pit_nv ON true
"""
_PIT_CITY = """
    LEFT JOIN LATERAL (
        SELECT pnh.navn FROM "{staging}".dar_postnummer_hist pnh
        WHERE pnh.id = h.postnummer AND pnh.registreringtil IS NULL
          AND (pnh.virkningfra IS NULL OR pnh.virkningfra <= a.virkningfra)
          AND (pnh.virkningtil IS NULL OR pnh.virkningtil > a.virkningfra)
        ORDER BY pnh.virkningfra DESC NULLS LAST LIMIT 1
    ) pit_pn ON true
"""

_HIST_TABLES = ("dar_navngivenvej_hist", "dar_postnummer_hist")


def hist_index_sql(staging: str) -> list[str]:
    # dlt only indexes _dlt_id; the pit laterals need per-row id probes
    return [f'CREATE INDEX IF NOT EXISTS {t}_id_idx ON "{staging}".{t} (id)' for t in _HIST_TABLES]


async def ensure_hist_indexes(conn: asyncpg.Connection, *, staging: str = STAGING) -> None:
    """create the id btree the point-in-time laterals probe; skip a hist table absent this gen."""
    for table, ddl in zip(_HIST_TABLES, hist_index_sql(staging), strict=True):
        if await has_table(conn, f"{staging}.{table}"):
            await conn.execute(ddl)


# adgangspunkt is joined for the district stamp too; here it also carries the address geometry. the
# jordstykke CASE mirrors "keep the parcel ref only if it points at a served matrikel row".
_INNER_SQL = """
SELECT
    a.id                      AS id,
    nv.vejnavn                AS street_name,
    {pit_street}              AS pit_street,
    h.husnummertekst          AS husnummertekst,
    sb.navn                   AS sub_locality,
    a.etage                   AS floor,
    a.door                    AS door,
    pn.postnr                 AS postcode,
    pn.navn                   AS city,
    {pit_city}                AS pit_city,
    {lifecycle}               AS lifecycle,
    ap.x                      AS adgangspunkt_x,
    ap.y                      AS adgangspunkt_y,
    vp.x                      AS vejpunkt_x,
    vp.y                      AS vejpunkt_y,
    k.code                    AS kommunekode,
    r.code                    AS regionskode,
    sg.code                   AS sognekode,
    ds.retskredsnummer        AS retskredsnummer,
    ds.politikredsnummer      AS politikredsnummer,
    ds.opstillingskredsnummer AS opstillingskredsnummer,
    CASE WHEN m.jordstykke IS NOT NULL THEN h.jordstykke END AS jordstykke,
    COALESCE(unit.bfe, m.bfe) AS ejendom_bfe
FROM "{staging}".dar_adresse a
JOIN "{staging}".dar_husnummer h
    ON h.id = a.husnummer AND h._deleted IS NOT TRUE
LEFT JOIN "{staging}".dar_navngivenvej nv
    ON nv.id = h.navngivenvej AND nv._deleted IS NOT TRUE
LEFT JOIN "{staging}".dar_postnummer pn
    ON pn.id = h.postnummer AND pn._deleted IS NOT TRUE
LEFT JOIN "{staging}".dar_supplerendebynavn sb
    ON sb.id = h.supplerende_bynavn AND sb._deleted IS NOT TRUE
LEFT JOIN "{staging}".dar_adressepunkt ap
    ON ap.id = h.adgangspunkt AND ap._deleted IS NOT TRUE
LEFT JOIN "{staging}".dar_adressepunkt vp
    ON vp.id = h.vejpunkt AND vp._deleted IS NOT TRUE
LEFT JOIN "{staging}".dagi_kommuneinddeling k
    ON k.id = h.kommuneinddeling AND k._deleted IS NOT TRUE
LEFT JOIN "{staging}".dagi_regionsinddeling r
    ON r.id = k.region_lokalid AND r._deleted IS NOT TRUE
LEFT JOIN "{staging}".dagi_sogneinddeling sg
    ON sg.id = h.sogneinddeling AND sg._deleted IS NOT TRUE
LEFT JOIN "{schema}"._district_stamp ds
    ON ds.husnummer_id = h.id
LEFT JOIN "{schema}".matrikel m
    ON m.jordstykke = h.jordstykke
LEFT JOIN (
    SELECT eb.adresse_lokalid, min(eb.bfe) AS bfe  -- validate membership+type BEFORE dedup
    FROM "{staging}".ebr_ejendomsbeliggenhed eb
    JOIN "{schema}".ejendom u ON u.bfe = eb.bfe AND u.type = 'ejerlejlighed'
    WHERE eb._deleted IS NOT TRUE AND eb.adresse_lokalid IS NOT NULL
    GROUP BY eb.adresse_lokalid
) unit ON unit.adresse_lokalid = a.id
{pit_joins}
WHERE a._deleted IS NOT TRUE
"""

# a retired/abandoned address renders its point-in-time name; a current one is unaffected
_OUTER_SQL = """
SELECT
    sub.id,
    CASE WHEN sub.lifecycle = ANY($1) THEN COALESCE(sub.pit_street, sub.street_name)
         ELSE sub.street_name END AS street_name,
    sub.husnummertekst, sub.sub_locality, sub.floor, sub.door, sub.postcode,
    CASE WHEN sub.lifecycle = ANY($1) THEN COALESCE(sub.pit_city, sub.city)
         ELSE sub.city END AS city,
    sub.lifecycle,
    sub.adgangspunkt_x, sub.adgangspunkt_y, sub.vejpunkt_x, sub.vejpunkt_y,
    sub.kommunekode, sub.regionskode, sub.sognekode,
    sub.retskredsnummer, sub.politikredsnummer, sub.opstillingskredsnummer,
    sub.jordstykke, sub.ejendom_bfe
FROM ({inner}) sub
"""


def address_sql(staging: str, schema: str, *, nv_hist: bool = False, pn_hist: bool = False) -> str:
    lifecycle = dar_case("a.status", "a.virkningfra", "a.virkningtil")
    inner = _INNER_SQL.format(
        staging=staging,
        schema=schema,
        lifecycle=lifecycle,
        pit_street="pit_nv.vejnavn" if nv_hist else "NULL",
        pit_city="pit_pn.navn" if pn_hist else "NULL",
        pit_joins=(_PIT_STREET.format(staging=staging) if nv_hist else "")
        + (_PIT_CITY.format(staging=staging) if pn_hist else ""),
    )
    return _OUTER_SQL.format(inner=inner)


def _row_to_record(r: asyncpg.Record) -> dict | None:
    """the per-address corpus dict + lifecycle; None to drop a row with no street, no postcode, or
    an unsplittable husnr (unservable; the count floors catch a mass drop)."""
    street = r["street_name"]
    number, letter = split_husnr(r["husnummertekst"] or "")
    postcode = clean(r["postcode"])
    if not street or not number or not postcode:
        return None
    return {
        "id": r["id"],
        "street_name": street.strip(),
        "house_number": number,
        "house_letter": letter,
        "floor": clean(r["floor"]),
        "door": clean(r["door"]),
        "sub_locality": clean(r["sub_locality"]),
        "postcode": postcode,
        "city": clean(r["city"]),
        "lifecycle": r["lifecycle"],
        "adgangspunkt_x": r["adgangspunkt_x"],
        "adgangspunkt_y": r["adgangspunkt_y"],
        "vejpunkt_x": r["vejpunkt_x"],
        "vejpunkt_y": r["vejpunkt_y"],
        "kommunekode": r["kommunekode"],
        "regionskode": r["regionskode"],
        "sognekode": r["sognekode"],
        "retskredsnummer": r["retskredsnummer"],
        "politikredsnummer": r["politikredsnummer"],
        "opstillingskredsnummer": r["opstillingskredsnummer"],
        "jordstykke": r["jordstykke"],
        "ejendom_bfe": r["ejendom_bfe"],
    }


async def iter_addresses(
    reader: asyncpg.Connection, schema: str, *, staging: str = STAGING
) -> AsyncIterator[dict]:
    """stream the address join as corpus record dicts (the shape train/gen + aux consume too), with
    lifecycle + point-in-time names where the *_hist tables are staged."""
    nv_hist = await has_table(reader, f"{staging}.dar_navngivenvej_hist")
    pn_hist = await has_table(reader, f"{staging}.dar_postnummer_hist")
    sql = address_sql(staging, schema, nv_hist=nv_hist, pn_hist=pn_hist)
    async for r in stream(reader, sql, list(_HIST_LIFECYCLES)):
        rec = _row_to_record(r)
        if rec is not None:
            yield rec
