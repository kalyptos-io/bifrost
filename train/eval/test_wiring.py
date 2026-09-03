"""tripwire for the two db-bound eval entrypoints. neither is reached by any other test, so an app
signature change rots them silently (it did, twice). db-free: the outbound calls are recorded off a
fake source, then re-bound against the real signatures.
"""

from __future__ import annotations

import inspect
import json

import bifrost.eval_adapter as eval_adapter
from bifrost.arms.repository import SourceSnapshot
from bifrost.composition import resolve_request
from bifrost.core.types import AddressRow, Axis, Belief, Capability, Grade

from . import snapshot


class _Snapshot:
    """stands in for SourceSnapshot: an async ctx manager carrying a per-generation resolution."""

    resolution = object()

    async def __aenter__(self) -> _Snapshot:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Source:
    def snapshot(self) -> _Snapshot:
        return _Snapshot()


def test_entrypoints_import() -> None:
    assert callable(eval_adapter.resolver) and callable(snapshot.main)


def test_snapshot_items_are_bounded_and_repeatable(tmp_path) -> None:
    source = tmp_path / "synth.jsonl"
    source.write_text(
        "".join(
            json.dumps({"raw": str(i), "target": {"id": str(i)}, "mutations": []}) + "\n"
            for i in range(10)
        ),
        encoding="utf-8",
    )

    first = snapshot._items(str(source), 4, 7)

    assert first == snapshot._items(str(source), 4, 7)
    assert len(first) == 4


def test_connect_sites_build_the_resolution() -> None:
    # branches are per-generation; a connect without the factory serves resolution=None
    for fn in (eval_adapter._ensure, snapshot._run):
        assert "resolution_factory=build_resolution" in inspect.getsource(fn), fn.__qualname__


async def test_eval_adapter_resolve_call_binds() -> None:
    calls: list[tuple] = []

    async def recorder(*args, **kwargs) -> None:
        calls.append((args, kwargs))

    real_fn, real_source = eval_adapter.resolve_request, eval_adapter._source
    eval_adapter.resolve_request, eval_adapter._source = recorder, _Source()
    try:
        await eval_adapter._resolve_one("vestergade 1 8000 aarhus c")
    finally:
        eval_adapter.resolve_request, eval_adapter._source = real_fn, real_source

    ((args, kwargs),) = calls
    inspect.signature(resolve_request).bind(*args, **kwargs)  # every required param still supplied
    assert kwargs["resolution"] is _Snapshot.resolution


_ROW = AddressRow("a1", 0, "Vestergade", "vestergade", "1", None, None, None, "8000", None, 1.0)


class _RecordingSource:
    """fake SourceSnapshot: serves one stream batch and an empty recovery set, keeping the calls."""

    def __init__(self) -> None:
        self.calls: dict[str, tuple] = {}

    async def street_stream(self, *args, **kwargs):
        self.calls["street_stream"] = (args, kwargs)
        yield [_ROW]

    async def by_postcodes(self, *args, **kwargs) -> list[AddressRow]:
        self.calls["by_postcodes"] = (args, kwargs)
        return []


async def test_snapshot_pool_reads_through_the_engine() -> None:
    beliefs = (
        Belief(
            axis=Axis.STREET,
            value="vestergade",
            weight=1.0,
            grade=Grade.TRIGRAM,
            capability=Capability.SOURCE,
        ),
        Belief(
            axis=Axis.POSTCODE,
            value="8000",
            weight=1.0,
            grade=Grade.POSTCODE_FUZZY,
            capability=Capability.SOURCE,
            members=frozenset({"8000"}),
        ),
    )
    source = _RecordingSource()

    assert await snapshot._pool(beliefs, source) == [_ROW]  # the pool is what the engine touched

    assert set(source.calls) == {"street_stream", "by_postcodes"}
    for name, (args, kwargs) in source.calls.items():
        real = inspect.signature(getattr(SourceSnapshot, name))
        real.bind(None, *args, **kwargs)  # the proxy still speaks the real source's signature
    assert source.calls["street_stream"][1].get("collapse_units", False) is False
