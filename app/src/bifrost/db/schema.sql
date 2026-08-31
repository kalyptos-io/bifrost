-- bifrost schema: a thin star. addresses fact = FKs + intrinsic fields; street_dim feeds the in-proc
-- trigram index with its folded street (off the 3.9M-row fact); street_postcode is a pure id bridge.
-- NO MIGRATION FRAMEWORK: applied unqualified into a fresh gen_<ts> schema per load, never mutated.
-- reshape = a new generation (db/generations.py), not an in-place ALTER.

-- WITH SCHEMA public pins the extension there even under a gen search_path, so DROP SCHEMA gc can't
-- cascade it away; pg_trgm ops resolve via the public fallback in the serving search_path
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

CREATE TABLE IF NOT EXISTS addresses (          -- thin fact: 1 row per unit address
    address_id    text    PRIMARY KEY,
    street_id     integer NOT NULL,
    postcode      text    NOT NULL,             -- text not int: keeps leading-zero postcodes
    house_number  text    NOT NULL,
    house_letter  text,
    floor         text,
    door          text,
    sub_locality  text,                         -- display-only (renamed bynavn), unindexed
    -- access + road point, etrs89/utm32 (epsg:25832): easting/northing in metres, not lat/lon
    adgangspunkt_x double precision,
    adgangspunkt_y double precision,
    vejpunkt_x    double precision,
    vejpunkt_y    double precision,
    -- denormalized dagi area codes for /resolve projection; null = unstamped (projection abstains)
    kommunekode            text,
    regionskode            text,
    sognekode              text,
    retskredsnummer        text,
    politikredsnummer      text,
    opstillingskredsnummer text,
    jordstykke             text,  -- the parcel the address sits on; null = no jordstykke link
    ejendom_bfe            text,  -- projected-to ejendom (ejerlejlighed else ground sfe); null = unstamped
    city                   text,  -- display city; point-in-time for retired rows, current otherwise
    lifecycle              text NOT NULL DEFAULT 'current'  -- current|retired|preliminary|abandoned
);

CREATE TABLE IF NOT EXISTS street_dim (         -- off the fact; feeds in-proc trigram index
    street_id     integer PRIMARY KEY,
    street        text    NOT NULL,
    folded_street text    NOT NULL,             -- normalized/folded, trigram-scored
    lifecycle     text    NOT NULL DEFAULT 'current'  -- best lifecycle across the collapsed name's addresses
);

-- husnr recovery (postcode = ANY ∧ house_number =); leading col also serves the postcode-alone arms
CREATE INDEX IF NOT EXISTS addresses_postcode_house_number ON addresses (postcode, house_number);
-- street stream joins addresses USING (street_id, postcode) after the combo expansion
CREATE INDEX IF NOT EXISTS addresses_street_postcode ON addresses (street_id, postcode);

CREATE TABLE IF NOT EXISTS street_postcode (    -- pure (street_id, postcode) bridge, no string columns
    street_id integer NOT NULL,
    postcode  text    NOT NULL,
    PRIMARY KEY (street_id, postcode)
);

-- physical road keyed by its dar navngivenvej id (the road's stable identity): one row per road,
-- one complete uncut (Multi)LineString. postcodes[] = the postcodes its addresses touch
-- (disambiguates same-named roads + confines a postcode-pinned query). geojson text, epsg:25832.
CREATE TABLE IF NOT EXISTS road (
    navngivenvej_id text    PRIMARY KEY,
    street_id       integer NOT NULL,           -- name-collapsed; links the trigram matcher to roads
    postcodes       text[]  NOT NULL,
    geometry        text,                        -- geojson (Multi)LineString; null where history lacks it
    lifecycle       text    NOT NULL DEFAULT 'current'
);
CREATE INDEX IF NOT EXISTS road_street_id ON road (street_id);

-- dagi admin/postal areas (kommune/region/sogn/postnummer). geometry is geojson text (epsg:25832),
-- no postgis: the area gazetteer ranks on folded_name in-process; geometry is fetched per hit.
CREATE TABLE IF NOT EXISTS admin_area (
    area_id     text PRIMARY KEY,
    kind        text NOT NULL,                  -- postcode|city|kommune|sogn|region
    code        text,                           -- postnr/kommunekode/etc; null where name is the key
    name        text NOT NULL,
    folded_name text NOT NULL,                  -- normalized/folded, trigram-scored
    geometry    text,                           -- geojson Polygon/MultiPolygon; null where history lacks it
    lifecycle   text NOT NULL DEFAULT 'current'
);

-- matriklen: one row per jordstykke (parcel) - the footprint a resolved address projects to
-- (/resolve target=matrikel) and is searched by BFE/ejerlavskode or betegnelse (/search target=matrikel).
-- geometry is geojson text, epsg:25832; fetched by the jordstykke PK, betegnelse via a trigram KNN.
CREATE TABLE IF NOT EXISTS matrikel (
    jordstykke         text PRIMARY KEY,          -- mat jordstykke id_lokalId (one parcel)
    bfe                text NOT NULL,             -- the samlet fast ejendom this parcel belongs to
    matrikelnummer     text,
    ejerlavskode       text,
    ejerlavsnavn       text,
    kommunekode        text,
    kommunenavn        text,
    centroid           text,                      -- "x y" epsg:25832 parcel centroid
    geometry           text,                      -- geojson Polygon/MultiPolygon; null on non-current parcels
    matrikelbetegnelse text,                      -- "<matrikelnummer> <ejerlavsnavn>" display
    folded_betegnelse  text,                      -- folded matrikelnummer + ejerlavsnavn + ejerlavskode, trigram-indexed
    lifecycle          text NOT NULL DEFAULT 'current'
);
-- betegnelse search: word-similarity trigram KNN over the folded label (matrikelnummer + ejerlav)
CREATE INDEX IF NOT EXISTS matrikel_betegnelse_trgm ON matrikel USING gist (folded_betegnelse gist_trgm_ops);
-- exact digit lookups: a pure-digit /search query hits bfe and ejerlavskode, merged bfe-first
CREATE INDEX IF NOT EXISTS matrikel_bfe ON matrikel (bfe);
CREATE INDEX IF NOT EXISTS matrikel_ejerlavskode ON matrikel (ejerlavskode);

-- danske stednavne: named places (sø/skov/bebyggelse/vej-not-in-dar/...). geometry is geojson text
-- (epsg:25832), point/line/polygon. searched by name only via an in-proc trigram gazetteer (like
-- admin_area); geometry fetched per hit by the pk. no postgis, no secondary index. search-only:
-- not a partition, never a /resolve target.
CREATE TABLE IF NOT EXISTS stednavne (
    stednavn_id text PRIMARY KEY,
    name        text NOT NULL,
    folded_name text NOT NULL,                  -- normalized/folded, trigram-scored
    type        text NOT NULL,                  -- place-name object type; returned, not filtered on
    geometry    text,                           -- geojson Point/LineString/Polygon; null where history lacks it
    lifecycle   text NOT NULL DEFAULT 'current'
);

-- bestemt fast ejendom: one row per bfe across all three property types (sfe, ejerlejlighed, bpfg)
-- so any bfe is a single pk probe. chain/children are parallel text[] (self -> ground; direct kids);
-- geometry is the pre-merged ground footprint, only for multi-parcel sfe. no secondary index: bfe is
-- the pk, betegnelse/ejerlav text search enters via the matrikel indexes.
CREATE TABLE IF NOT EXISTS ejendom (
    bfe                  text PRIMARY KEY,
    type                 text NOT NULL CHECK (type IN
        ('samlet_fast_ejendom', 'ejerlejlighed', 'bygning_paa_fremmed_grund')),
    parent_bfe           text,            -- direct legal parent; null at ground / dangling ref
    ground_bfe           text,            -- ground sfe (= bfe for sfe rows); null = truncated chain
    ejerlejlighedsnummer text,
    jordstykke           text,            -- representative ground parcel (min jordstykke)
    geometry             text,            -- pre-merged geojson MultiPolygon, multi-parcel sfe only
    chain_bfes           text[] NOT NULL, -- self -> ground incl. self, parallel with chain_types
    chain_types          text[] NOT NULL,
    children_bfes        text[] NOT NULL DEFAULT '{}',  -- direct children, uncapped in storage
    children_types       text[] NOT NULL DEFAULT '{}',
    lifecycle            text NOT NULL DEFAULT 'current',
    CHECK (cardinality(chain_bfes) = cardinality(chain_types)),
    CHECK (cardinality(chain_bfes) BETWEEN 1 AND 3),
    CHECK (chain_bfes[1] = bfe),
    CHECK (cardinality(children_bfes) = cardinality(children_types))
);

-- registry-derived aux maps, generation-scoped so they track the address data (was image-baked json).
-- loaded in full at startup like street_dim/street_postcode: no PK/index, grouped into maps in-proc.
CREATE TABLE IF NOT EXISTS aux_postcode_dim (    -- full sorted postcode dimension for fuzzy recovery
    postcode text NOT NULL
);

CREATE TABLE IF NOT EXISTS aux_city_map (        -- one row per (folded city, postcode)
    folded_name text NOT NULL,
    postcode    text NOT NULL
);

CREATE TABLE IF NOT EXISTS aux_subloc_map (      -- one row per (folded sub-locality, postcode)
    folded_name text NOT NULL,
    postcode    text NOT NULL
);

-- name-history aliases: a historical designation -> the canonical entity id, searchable via the
-- existing in-proc indexes (street/area). all tiny; loaded in full / unioned onto the canonical rows
-- at generation cutover. no PK: an id may carry several. (retired parcel betegnelser need no alias
-- table: matrikel retains its non-current rows, the history betegnelse KNN scans them directly.)
CREATE TABLE IF NOT EXISTS street_alias (        -- a road's prior vejnavn -> its canonical street_id
    name          text    NOT NULL,             -- the historical street name (display)
    folded_street text    NOT NULL,             -- normalized/folded, trigram-scored
    street_id     integer NOT NULL,
    postcodes     text[]  NOT NULL,             -- scoped to the renamed road's addresses (no fan-out)
    lifecycle     text    NOT NULL
);

CREATE TABLE IF NOT EXISTS area_alias (          -- a postdistrikt/area's prior name -> its area_id
    area_id     text NOT NULL,
    name        text NOT NULL,
    folded_name text NOT NULL,
    lifecycle   text NOT NULL
);
