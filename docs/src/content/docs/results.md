---
title: Results
description: Anatomy of a match - kind, components, postcodes, ejendom, geometry and meta.
---

Both endpoints return the same envelope: your `query`, echoed back, and a `matches` array ranked
best first. Every entry has the same shape, whatever it describes.

```json
{
	"kind": "address",
	"result": "Rådhuspladsen 1, 1550 København V",
	"lifecycle": "current",
	"components": { "street": "Rådhuspladsen", "house_number": "1", "postcode": "1550", "city": "København V" },
	"postcodes": null,
	"geometry": { "srid": 25832, "geojson": {}, "vejpunkt": [724454.63549553, 6175800.01960673] },
	"meta": { "score": 0.002198900732783531, "confidence": "A", "uuid": null }
}
```

:::caution[Coordinates are not lat/lon]
Every coordinate the API returns is EPSG:25832 (ETRS89 / UTM zone 32N), easting and northing in
metres. There is no WGS84 output anywhere in the API. `[724434.93, 6175755.61]` is a point in
Copenhagen.
:::

Most map libraries want degrees, so reproject on your side:

```python
from pyproj import Transformer

to_wgs84 = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
lon, lat = to_wgs84.transform(724434.93, 6175755.61)  # 12.569578, 55.675627
```

```js
import proj4 from 'proj4';

proj4.defs('EPSG:25832', '+proj=utm +zone=32 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs');
const [lon, lat] = proj4('EPSG:25832', 'WGS84', [724434.93, 6175755.61]);
```

## kind

`kind` is the discriminator. It says what the match describes, and which keys `components` carries.

| `kind` | What it is |
| --- | --- |
| `address` | A specific address. |
| `street` | A road. |
| `postcode` | A postnummer. |
| `city` | A postdistrikt. |
| `kommune` | A municipality. |
| `sogn` | A parish. |
| `region` | A region. |
| `retskreds` | A court district. |
| `politikreds` | A police district. |
| `opstillingskreds` | A nomination district. |
| `ejendom` | A property card (bestemt fast ejendom). |
| `stednavne` | A named place. `/search` only. |

## result

The display string for the match: the address line, the road name, the area name or the matrikel
betegnelse. Use `result` for display and `components` for logic.

## lifecycle

The state of the designation: `current`, `preliminary`, `retired` or `abandoned`. A request only
gets back the states it asked for.

## components

The parsed parts, keyed by kind. For an address, this is a subset of the eight address fields:

```json
{
	"street": "Nøddebogade",
	"house_number": "1",
	"floor": "st",
	"door": "th",
	"postcode": "2200",
	"city": "København N"
}
```

The full set is `street`, `house_number`, `house_letter`, `floor`, `door`, `postcode`, `city` and
`sub_locality`. Absent parts are omitted rather than set to `null`.

For an area, `components` carries the single key matching `kind`, such as `{"kommune": "København"}`
or `{"sogn": "Helligånds"}`. A `postcode` carries both `postcode` and `city`. A `stednavne` carries
the name and a `type` (`landskabsform`, `fortidsminde`, `bygning`, `vandløb`, `farvand`, `vej`,
`topografi`, and others). An `ejendom` carries the full matrikel card:

```json
{
	"bfe": "6033528",
	"jordstykke": "10438851",
	"matrikelnummer": "162",
	"ejerlavskode": "2000179",
	"ejerlavsnavn": "Vestervold Kvarter, København",
	"kommunekode": "0101",
	"kommunenavn": "København",
	"centroid": "724495.727 6175696.138",
	"matrikelbetegnelse": "162 Vestervold Kvarter, København"
}
```

## postcodes

The postcodes a road runs through. A road can span several postnumre, which is why this is not a
component.

```json
{ "kind": "street", "result": "Vestergade", "postcodes": ["8550"] }
```

`postcodes` is `null` for every kind other than `street`.

## ejendom

Present only when `kind` is `ejendom`. The key is omitted for every other kind. It carries the
property's legal nesting.

```json
{
	"bfe": "156329",
	"type": "ejerlejlighed",
	"ejerlejlighedsnummer": "14",
	"relations": {
		"parents": { "refs": [{ "bfe": "6022356", "type": "samlet_fast_ejendom" }], "complete": true },
		"children": { "refs": [], "complete": true }
	}
}
```

- `bfe` is the BFE number, the property's national identifier.
- `type` is `samlet_fast_ejendom`, `ejerlejlighed` or `bygning_paa_fremmed_grund`.
- `ejerlejlighedsnummer` is the unit number. The key is omitted, not `null`, when the property is
  not a unit.
- `relations.parents.refs` is the ancestry, nearest first and ground last, excluding the property
  itself. A unit's parent is the samlet fast ejendom it sits in.
- `relations.parents.complete` is `false` when the source data has a dangling legal link. That
  happens in the register, and it means the chain you got is partial.
- `relations.children.refs` are the direct children, capped at 1000. A block of flats returns every
  ejerlejlighed under it.
- `relations.children.complete` is `false` when that cap truncated the list.

## geometry

`null` when you requested `geometry: false`, or when the entity has no geometry in the register.
`/search` defaults to `false`; `/resolve` defaults to `true`.

```json
{
	"srid": 25832,
	"geojson": { "type": "Point", "coordinates": [724434.93, 6175755.61] },
	"vejpunkt": [724454.63549553, 6175800.01960673]
}
```

- `srid` is always `25832`.
- `geojson` is a bare GeoJSON geometry, not a Feature and not a FeatureCollection. The type
  depends on the kind: `Point` for an address, `MultiLineString` for a road, `Polygon` or
  `MultiPolygon` for an area or a property. Place names can be any of them.
- `vejpunkt` is the point on the road for an address, where the geojson point is the access point
  on the property. It is `null` for every other kind.

Area polygons are the bulk of any response that carries one, which is why `/search` leaves them
off unless you ask. They are also generalized for delivery: a Douglas-Peucker pass at a tolerance
of `sqrt(area)/2000`, so the error scales with the feature instead of being a fixed distance. The
area of a served polygon is within about 0.03% of the register's. Use the register itself if you
need survey-grade boundaries.

## meta

```json
{ "score": 0.002198900732783531, "confidence": "A", "uuid": null }
```

`score` is a signed belief score, not a probability and not a 0..1 similarity. Higher is better,
and negative values are common. Compare scores within a single response, not across queries: a
clean address resolve scores near zero, while a flat register match scores `1.0`.

`confidence` is `A`, `B` or `C`. `A` is a clean, unambiguous leader. `B` means the engine had to
work for it, through a street correction, a unit mismatch, or a lone candidate with no rival to
measure against. `C` marks a fuzzy or flat-field match. A response of all `C`s with negative scores
means the engine found something, but probably not your address.

`uuid` is the DAR identifier. It appears only when `/resolve` was called with `"uuid": true`, and it
is always `null` on `/search`.

## Response headers

Every successful response carries `X-Bifrost-Data-Updated`, the UTC time the data behind it was last
refreshed. Batch responses carry it too, on the response rather than per item.

```
x-bifrost-data-updated: 2026-08-12T03:14:22Z
```
