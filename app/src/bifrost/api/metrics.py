"""prometheus instrumentation. multiprocess-aware: uvicorn runs N workers, so /metrics must
aggregate across them via PROMETHEUS_MULTIPROC_DIR or rate() breaks across processes. unset
(local/tests) -> plain in-process registry."""

import os
from time import perf_counter

from fastapi import FastAPI, Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Histogram,
    generate_latest,
    multiprocess,
)
from starlette.types import ASGIApp, Receive, Scope, Send

REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "request latency by handler and status",
    ["handler", "status"],
)

_SKIP = {"/health", "/metrics"}
_MULTIPROC = os.environ.get("PROMETHEUS_MULTIPROC_DIR")


class PrometheusMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _SKIP:
            await self.app(scope, receive, send)
            return
        status = 500  # default covers an exception before response.start
        start = perf_counter()

        async def capture(message: dict) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            route = scope.get("route")  # set by the router; absent on 404 -> bounds cardinality
            handler = route.path if route else "unmatched"
            REQUEST_DURATION.labels(handler, str(status)).observe(perf_counter() - start)


async def _metrics(request: Request) -> Response:
    if _MULTIPROC:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    else:
        registry = REGISTRY
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


def setup(app: FastAPI) -> None:
    if _MULTIPROC:
        os.makedirs(_MULTIPROC, exist_ok=True)
    app.add_middleware(PrometheusMiddleware)
    app.add_route("/metrics", _metrics)
