---
title: Introduction
description: What the Bifrost API answers, how to reach it, and which endpoint to use.
---

Bifrost resolves Danish address text into structured, geocoded results, and looks up the public
registers an address belongs to. You send address text, however messy it is, and you get back
components, a lifecycle state, a geometry and a score.

There are two endpoints. Both are `POST`, both take JSON and return JSON.

| Endpoint | Use it for |
| --- | --- |
| [`/resolve`](./resolve/) | Free-text or pre-pinned address input, resolved through the belief engine. Can answer with the postcode area, kommune or property instead of the address. |
| [`/search`](./search/) | A flat lookup in one named register: streets, postcodes, cities, properties, place names and administrative areas. No address parsing. |

Use `/resolve` when the input is an address. Use `/search` when you already know which register you
want and only need to match a name or a code in it. Both read the public Danish registers, which
update daily. See [Data sources](./data-sources/) for the licence and the update cycle.

## Base URL

```
https://bifrost.kalyptos.io
```

Both endpoints sit at the root: `/resolve` and `/search`.

## Making a request

There is no authentication and no API key. Send `Content-Type: application/json` and you get JSON
back. CORS is open for `GET` and `POST`, so you can call the API from the browser.

```sh
curl -sX POST https://bifrost.kalyptos.io/resolve \
  -H 'content-type: application/json' \
  -d '{"query": "Rådhuspladsen 1, 1550 København V"}'
```

Both endpoints accept a single query or an array of them. See [batch mode](./resolve/#batch-mode).

## Coordinates

Every geometry the API returns is EPSG:25832 (ETRS89 / UTM zone 32N): easting and northing in
metres. There is no WGS84 output and no lat/lon. See [Results](./results/).

## OpenAPI

The OpenAPI document is served at
[`/openapi.json`](https://bifrost.kalyptos.io/openapi.json), with Swagger UI at
[`/swagger`](https://bifrost.kalyptos.io/swagger). These pages describe the same contract in prose.
Generate your clients from the spec.
