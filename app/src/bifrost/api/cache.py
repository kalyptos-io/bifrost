"""response cache over dragonfly (redis protocol). owned by the api lifespan, never seen by core.
fail-open: any cache error degrades to a miss/no-op so resolve always answers."""

import logging

from pydantic import TypeAdapter
from redis.asyncio import Redis

from bifrost.core.types import Resolution

_log = logging.getLogger("bifrost.cache")
# frozen-dataclass + StrEnum + tuple round-trip as raw json bytes; one adapter over the unified
# payload (address belief result or geo feature result - exactly one is set)
_ADAPTER = TypeAdapter(Resolution)
_MAX_CONNECTIONS = 64  # per-worker redis pool ceiling; well above the batch sem + interactive load
_TIMEOUT = 0.25  # connect + socket secs; a blackholed cache must fail open fast, not stall a call


class ResponseCache:
    def __init__(self, client: Redis, ttl: int) -> None:
        self._client = client
        self._ttl = ttl

    @classmethod
    async def connect(cls, url: str, ttl: int) -> "ResponseCache":
        # lazy bounded pool; a dead url or pool exhaustion surfaces on get/set/mget as a miss
        return cls(
            Redis.from_url(
                url,
                max_connections=_MAX_CONNECTIONS,
                socket_connect_timeout=_TIMEOUT,
                socket_timeout=_TIMEOUT,
            ),
            ttl,
        )

    async def get(self, key: str) -> Resolution | None:
        try:
            raw = await self._client.get(key)
            return _ADAPTER.validate_json(raw) if raw else None
        except Exception as e:  # fail-open: cache down OR stale/foreign payload -> miss
            _log.warning("[!] cache get failed: %s", e)
            return None

    async def mget(self, keys: list[str]) -> list[Resolution | None]:
        if not keys:
            return []
        try:
            raws = await self._client.mget(keys)
        except Exception as e:  # fail-open: cache down -> whole batch misses
            _log.warning("[!] cache mget failed: %s", e)
            return [None] * len(keys)
        out: list[Resolution | None] = []
        for raw in raws:
            try:
                out.append(_ADAPTER.validate_json(raw) if raw else None)
            except Exception as e:  # fail-open per key: stale/foreign payload -> miss
                _log.warning("[!] cache mget decode failed: %s", e)
                out.append(None)
        return out

    async def set(self, key: str, value: Resolution) -> None:
        try:
            await self._client.set(key, _ADAPTER.dump_json(value), ex=self._ttl)
        except Exception as e:  # fail-open: store is best-effort
            _log.warning("[!] cache set failed: %s", e)

    async def close(self) -> None:
        await self._client.aclose()
