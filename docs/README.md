# docs

The public API reference at <https://bifrost.kalyptos.io/docs>. Astro + Starlight, served as a
static site from its own image. `docs/benchmarks/` holds the permanent benchmark results and their
provenance manifests.

```sh
cd docs && npm install
npm run dev        # local preview
npm run build      # static build into dist/
```

`DOCS_BASE` sets the path the assets are built against, so a change means a new image. The examples
are written against the production host and rewritten at runtime to `window.__BIFROST_API__`.
