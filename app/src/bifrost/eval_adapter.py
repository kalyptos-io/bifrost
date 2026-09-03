"""eval-only resolver: a sync (str)->Resolution over the async composition root, for the recall@k
harness. injected by `--resolver bifrost.eval_adapter:resolver` so train/eval/run.py never imports
the app or the db. owns one PostgresAddressSource on a dedicated loop thread, reused across calls.
"""

import asyncio
import threading
from dataclasses import dataclass

from bifrost.arms import segmenter
from bifrost.arms.repository import NotSeeded, PostgresAddressSource
from bifrost.composition import build_resolution, resolve_request
from bifrost.config import Settings


@dataclass(frozen=True, slots=True)
class Resolution:
    """duck-compatible with eval.run.Resolution (ids best-first, A/B/C category)."""

    ids: list[str]
    category: str | None = None


_loop: asyncio.AbstractEventLoop | None = None
_source: PostgresAddressSource | None = None
_lock = threading.Lock()


def _ensure() -> None:
    # asyncpg pool binds to its creating loop; build on a persistent thread + reuse both
    # (a fresh asyncio.run() per query would orphan it)
    global _loop, _source
    if _source is not None:
        return
    with _lock:
        if _source is not None:
            return
        segmenter.load()  # fail-fast on a missing artifact, like the api lifespan
        s = Settings()
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        try:
            source = asyncio.run_coroutine_threadsafe(
                PostgresAddressSource.connect(
                    s.database_dsn,
                    host=s.database_host,
                    min_size=s.db_pool_min,
                    max_size=s.db_pool_max,
                    refresh_interval=None,  # one-shot harness; no reseed poll
                    resolution_factory=build_resolution,
                ),
                loop,
            ).result()
        except NotSeeded:  # fail loud against an unseeded db, not silent empty results
            loop.call_soon_threadsafe(loop.stop)
            raise RuntimeError("database not seeded") from None
        except BaseException:
            loop.call_soon_threadsafe(loop.stop)  # don't orphan the loop thread on a failed connect
            raise
        _loop = loop  # publish before _source: the unlocked fast-path keys on _source
        _source = source


async def _resolve_one(query: str):
    # pin one snapshot per query so reads share a generation (a one-shot harness never cuts over)
    assert _source is not None
    async with _source.snapshot() as snap:
        assert snap.resolution is not None
        return await resolve_request(
            query,
            None,
            project="address",
            source=snap,
            geo_source=snap,
            resolution=snap.resolution,
        )


def resolver(query: str) -> Resolution:
    _ensure()
    assert _loop is not None and _source is not None
    # project=address: full-address recall@k, never the geo feature path
    resolution = asyncio.run_coroutine_threadsafe(_resolve_one(query), _loop).result()
    address = resolution.address
    if address is None or not address.matches:
        return Resolution(ids=[])
    return Resolution(
        ids=[m.candidate.address_id for m in address.matches],
        category=address.matches[0].confidence.value,
    )
