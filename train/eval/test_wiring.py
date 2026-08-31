"""tripwire for the two db-bound eval entrypoints. neither is reached by any other test, so an app
signature change rots them silently (it did, twice). db-free: the outbound calls are recorded off a
fake source, then re-bound against the real signatures.
"""

from __future__ import annotations

import inspect
import json

import bifrost.eval_adapter as eval_adapter
from bifrost.composition import resolve_request
from bifrost.core.merge import _recovery_fetches
from bifrost.core.types import Axis, Belief, Capability, Grade

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


async def test_snapshot_recovery_fetches_pass_a_lifecycle() -> None:
    calls: list[tuple] = []

    def recorder(*args, **kwargs) -> tuple:
        calls.append((args, kwargs))
        return ()

    beliefs = (
        Belief(
            axis=Axis.POSTCODE,
            value="8000",
            weight=1.0,
            grade=Grade.POSTCODE_FUZZY,
            capability=Capability.SOURCE,
            members=frozenset({"8000"}),
        ),
    )
    real_fn = snapshot._recovery_fetches
    snapshot._recovery_fetches = recorder
    try:
        assert await snapshot._pool(beliefs, _Source()) == []
    finally:
        snapshot._recovery_fetches = real_fn

    ((args, kwargs),) = calls
    bound = inspect.signature(_recovery_fetches).bind(*args, **kwargs)
    bound.apply_defaults()
    lifecycle = bound.arguments["lifecycle"]
    assert isinstance(lifecycle, tuple) and all(isinstance(v, str) for v in lifecycle)
