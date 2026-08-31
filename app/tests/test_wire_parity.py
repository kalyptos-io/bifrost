"""wire response snapshots for single, batch, geometry, and identifier variants."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import _wire_cases as wc
import pytest
from bifrost.api import main

_SNAPSHOTS = Path(__file__).parent / "wire_snapshots"


class _StubSnap:
    generation = "g0"  # endpoints read snap.generation to namespace cache keys
    seeded_at = datetime(2026, 8, 12, 3, 14, 22, tzinfo=UTC)  # freshness header; body unaffected
    resolution = None  # resolve_request is stubbed, so branches/merge ctx go unused

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _StubSource:
    def snapshot(self):
        return _StubSnap()


def _request():
    # endpoints pin a snapshot per request; a stub suffices (cache is off here)
    source = _StubSource()
    rt = main._Runtime(cache=None, batch_sem=asyncio.Semaphore(4), inflight={})
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(source=source, rt=rt)))


@pytest.mark.parametrize("case", wc.cases(), ids=lambda c: c.name)
async def test_wire_bytes_identical(case, monkeypatch):
    if case.endpoint == "resolve":
        monkeypatch.setattr(main, "resolve_request", case.fake)
        resp = await main.resolve_endpoint(main.AddressRequest(**case.payload), _request())
    else:
        monkeypatch.setattr(main, "search_request", case.fake)
        resp = await main.search_endpoint(main.SearchRequest(**case.payload), _request())

    assert resp.body == (_SNAPSHOTS / f"{case.name}.json").read_bytes()
