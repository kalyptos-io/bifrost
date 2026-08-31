# app

The deployable product: FastAPI over the belief engine. Simplified hexagonal - a pure `core`
(`normalize -> decompose -> beliefs -> merge -> select`) plus detachable arms. API reference:
<https://bifrost.kalyptos.io/docs>.

`POST /resolve` takes a messy Danish address (`query` free-text, or pinned `components`) and returns
the resolved address, or a named part of it with `project`. `POST /search` takes a `query` and a
`target` register. Both take one query or a batch, and both return the same `Match` shape with A/B/C
confidence and optional geojson `geometry`.

```sh
# resolve a noisy address (project defaults to "address")
curl localhost:8000/resolve -d '{"query":"nørrebrogade 12 2200 kbh"}'

# project the address to a layer it sits in (street | postcode | city | ejendom | kommune | sogn |
# region | retskreds | politikreds | opstillingskreds; auto = deepest feature)
curl localhost:8000/resolve -d '{"query":"randersgade, københavn","project":"postcode"}'

# look up one register by name or code (the layers above, plus stednavne)
curl localhost:8000/search  -d '{"query":"vor frue","target":"sogn"}'
```

Every 200 response carries `X-Bifrost-Data-Updated`, the `seeded_at` UTC timestamp of the pinned
generation. It is a header and not a field, because a batch response is a bare array with no
envelope. The API reference gives the full contract.
