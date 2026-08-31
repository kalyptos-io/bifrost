# ui

A SvelteKit demo SPA with a resolver and a UTM32N canvas map that traces the result geometry. It
calls the API from the browser and is not on the request path. It ships as its own image and Helm
resources (`ui.enabled`, default on). Behind the chart Ingress it uses the same host as the API, so
the browser can use relative URLs.

```sh
cd ui && npm install
npm run dev      # vite dev; for a cross-origin api set window.__BIFROST_API__
npm run build    # static build into build/
```
