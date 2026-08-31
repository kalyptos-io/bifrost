"""build-time data contract: the COPY column tuples + the packaged schema.sql, shared by the
bifrost-sync loader and the shape fingerprint. no I/O beyond schema_sql()."""

from __future__ import annotations

from importlib import resources


def schema_sql() -> str:
    # packaged schema; applied unqualified into a fresh gen_<ts> schema per load, never mutated
    return (resources.files("bifrost.db") / "schema.sql").read_text("utf-8")


# addresses fact column order; the positional contract for the COPY loader and the test seed tuples
ADDRESS_COLUMNS = (
    "address_id",
    "street_id",
    "postcode",
    "house_number",
    "house_letter",
    "floor",
    "door",
    "sub_locality",
    "adgangspunkt_x",
    "adgangspunkt_y",
    "vejpunkt_x",
    "vejpunkt_y",
    "kommunekode",
    "regionskode",
    "sognekode",
    "retskredsnummer",
    "politikredsnummer",
    "opstillingskredsnummer",
    "jordstykke",
    "ejendom_bfe",
    "city",  # point-in-time for retired rows, current otherwise
    "lifecycle",  # current|retired|preliminary|abandoned
)

# street_dim column order; the COPY contract for the dimension table
STREET_DIM_COLUMNS = ("street_id", "street", "folded_street", "lifecycle")

# physical road keyed by navngivenvej id; COPY contract for the road table
ROAD_COLUMNS = ("navngivenvej_id", "street_id", "postcodes", "geometry", "lifecycle")

# dagi admin-area COPY contract; geometry is geojson text
ADMIN_AREA_COLUMNS = ("area_id", "kind", "code", "name", "folded_name", "geometry", "lifecycle")

# matrikel (jordstykke) COPY contract; geometry is geojson text, centroid is "x y" (epsg:25832)
MATRIKEL_COLUMNS = (
    "jordstykke",
    "bfe",
    "matrikelnummer",
    "ejerlavskode",
    "ejerlavsnavn",
    "kommunekode",
    "kommunenavn",
    "centroid",
    "geometry",
    "matrikelbetegnelse",
    "folded_betegnelse",
    "lifecycle",
)

# danske stednavne (named places) COPY contract; geometry is geojson text (point/line/polygon)
STEDNAVNE_COLUMNS = ("stednavn_id", "name", "folded_name", "type", "geometry", "lifecycle")

# ejendom shape contract; SQL-derived on the writer (not COPY'd), still fingerprinted
EJENDOM_COLUMNS = (
    "bfe",
    "type",
    "parent_bfe",
    "ground_bfe",
    "ejerlejlighedsnummer",
    "jordstykke",
    "geometry",
    "chain_bfes",
    "chain_types",
    "children_bfes",
    "children_types",
    "lifecycle",
)

# name-alias COPY contracts (historical designations -> canonical id, searchable via the in-proc
# street/area indexes). all tiny, derived at snapshot time.
STREET_ALIAS_COLUMNS = ("name", "folded_street", "street_id", "postcodes", "lifecycle")
AREA_ALIAS_COLUMNS = ("area_id", "name", "folded_name", "lifecycle")

# aux full sorted postcode dimension COPY contract
AUX_POSTCODE_DIM_COLUMNS = ("postcode",)

# aux city gazetteer COPY contract; one row per (folded city, postcode)
AUX_CITY_MAP_COLUMNS = ("folded_name", "postcode")

# aux sub-locality gazetteer COPY contract; one row per (folded sub-locality, postcode)
AUX_SUBLOC_MAP_COLUMNS = ("folded_name", "postcode")
