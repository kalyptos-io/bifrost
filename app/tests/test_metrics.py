"""middleware records latency under the matched route, skips probes/scrapes. drives the asgi
callable directly (no httpx); reads the default in-process registry (no multiproc env in tests)."""

from types import SimpleNamespace

from bifrost.api.metrics import PrometheusMiddleware
from prometheus_client import REGISTRY


def _count(handler: str, status: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "http_request_duration_seconds_count", {"handler": handler, "status": status}
        )
        or 0.0
    )


async def _drive(mw: PrometheusMiddleware, path: str) -> None:
    scope = {"type": "http", "path": path, "method": "GET", "headers": []}

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_: dict) -> None: ...

    await mw(scope, receive, send)


async def _ok_app(scope, receive, send) -> None:
    scope["route"] = SimpleNamespace(path="/resolve")  # emulate the router
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_records_matched_route():
    mw = PrometheusMiddleware(_ok_app)
    before = _count("/resolve", "200")
    await _drive(mw, "/resolve")
    assert _count("/resolve", "200") == before + 1


async def test_skips_probe_path():
    mw = PrometheusMiddleware(_ok_app)
    await _drive(mw, "/health")  # in _SKIP -> no observation
    assert (
        REGISTRY.get_sample_value(
            "http_request_duration_seconds_count", {"handler": "/health", "status": "200"}
        )
        is None
    )
