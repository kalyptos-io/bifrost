"""FastAPI driving adapter. lifespan owns the source + response cache; requests run via the
composition root. two data endpoints: `POST /resolve` (the address engine; str/list query runs the
segmenter, components pin, `project` picks the report altitude) and `POST /search` (a flat register
lookup by name/code: query + target, no segmenter, no merge)."""

import asyncio
import hashlib
import json
import logging
import os
import re
import ssl
import sys
from collections.abc import Awaitable, Callable, Hashable, Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import asyncpg
import pydantic_core
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from bifrost.api import metrics
from bifrost.api.cache import ResponseCache
from bifrost.api.contract import (
    DATA_UPDATED_HEADER,
    AddressError,
    AddressRequest,
    AddressResult,
    ResolveItem,
    SearchItem,
    SearchRequest,
    data_updated,
    to_result,
)
from bifrost.api.limits import LimitsMiddleware
from bifrost.arms import segmenter
from bifrost.arms.normalize import normalize
from bifrost.arms.repository import NotSeeded, PostgresAddressSource, SourceSnapshot
from bifrost.composition import build_resolution, resolve_request, search_request
from bifrost.config import Settings
from bifrost.core.geo import search_cache_token
from bifrost.core.types import Resolution, rank_width

_log = logging.getLogger("bifrost.api")

_INIT_RETRY = 5.0  # secs between source connect attempts while no matching generation exists
# bad credentials/tls never self-heal; retrying them forever hides a broken rollout behind unready
_FATAL_CONNECT = (asyncpg.InvalidAuthorizationSpecificationError, ssl.SSLError)


@dataclass(frozen=True, slots=True)
class _Runtime:
    # per-app request-handling state: response cache, batch pool guard, singleflight map
    cache: ResponseCache | None
    batch_sem: asyncio.Semaphore
    inflight: dict[str, asyncio.Future[Resolution]]


async def _init_source(app: FastAPI, settings: Settings) -> None:
    # sit unready until a generation matching the build shape exists, then publish. a shape-bumped
    # image validly precedes its generation, so "not seeded" is transient - never fatal, no restart
    while True:
        try:
            app.state.source = await PostgresAddressSource.connect(
                settings.database_dsn,
                host=settings.database_host,
                min_size=settings.db_pool_min,
                max_size=settings.db_pool_max,
                resolution_factory=build_resolution,
            )
            _log.info("[+] address source ready")
            return
        except NotSeeded:
            _log.info("[i] no matching generation yet, retrying")
            await asyncio.sleep(_INIT_RETRY)
        except _FATAL_CONNECT as e:
            _log.error("[-] fatal database connection error: %s", e)
            os._exit(1)  # a task raise would only strand the pod unready; fail the rollout instead
        except Exception as e:  # conn/transient: keep waiting for the db to come up
            _log.info("[i] db not ready, retrying: %s", e)
            await asyncio.sleep(_INIT_RETRY)


def _raw_geometries(content: Any) -> Iterator[dict]:
    # to_result tree: feature geojson is a pre-serialized str (spliced raw), address path a dict
    items = content if isinstance(content, list) else [content]
    for it in items:
        if not isinstance(it, dict):
            continue
        for m in it.get("matches") or ():
            g = m.get("geometry") if isinstance(m, dict) else None
            if isinstance(g, dict) and isinstance(g.get("geojson"), str):
                yield g


class _JSONResponse(JSONResponse):
    # rust encoder ~5x faster than stdlib json.dumps on large geojson responses. feature geojson is
    # already-serialized text: splice it raw (to_json would double-encode a str) instead of parsing
    def render(self, content: Any) -> bytes:
        sentinel, raws = uuid4().hex, []  # per-response prefix, never escaped; collision-proof
        tokens: dict[int, str] = {}  # a deduped batch aliases one dict at n positions: splice once
        for g in _raw_geometries(content):
            if (token := tokens.get(id(g))) is None:
                raws.append(g["geojson"].encode())
                tokens[id(g)] = token = f"{sentinel}{len(raws) - 1}"
            g["geojson"] = token  # transient tree: render owns the only ref
        body = pydantic_core.to_json(content)
        if not raws:
            return body
        # one split on the quoted sentinel tokens; the captured index picks each raw to interleave
        parts = re.split(b'"' + sentinel.encode() + rb'(\d+)"', body)
        return b"".join(raws[int(p)] if i % 2 else p for i, p in enumerate(parts))


router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    # pure liveness: an unseeded/shape-bumped pod is transient-unready (/ready), never restarted
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict[str, str]:
    # a served generation is the whole readiness contract; a failing serving lease is an alert
    # (bifrost_serving_lease_failures_total), never a reason to drop a pod that resolves correctly
    if request.app.state.source is None:
        raise HTTPException(status_code=503, detail="warming up")
    return {"status": "ready"}


class _LeaderGone(Exception):
    """singleflight leader cancelled before producing; the waiters recompute."""


async def _cached(
    rt: _Runtime,
    key: str,
    factory: Callable[[], Awaitable[Resolution]],
    *,
    known_miss: bool = False,  # set by the batch path, whose mget already read this key
) -> Resolution:
    # fail-open get/compute/set: cache the rich engine Resolution; the factory runs only on a miss
    cache, inflight = rt.cache, rt.inflight
    if cache and not known_miss and (hit := await cache.get(key)) is not None:
        return hit
    while (fut := inflight.get(key)) is not None:
        # concurrent miss: shield keeps a cancelled follower from cancelling the shared future
        try:
            return await asyncio.shield(fut)
        except _LeaderGone:
            continue  # leader's own request went away; the first waiter back here takes over
    inflight[key] = fut = asyncio.get_running_loop().create_future()
    try:
        resolution = await factory()
    except asyncio.CancelledError:
        fut.set_exception(_LeaderGone())
        fut.exception()
        raise
    except BaseException as exc:
        fut.set_exception(exc)
        fut.exception()  # retrieved so a waiter-less failure doesn't warn
        raise
    else:
        fut.set_result(resolution)
    finally:
        del inflight[key]
    if cache:
        await cache.set(key, resolution)  # after the handoff: a cancel here must not strand waiters
    return resolution


def _canon_lifecycle(values: list[str]) -> tuple[str, ...]:
    return tuple(sorted(values))  # validated unique; sorted tuple is the canonical cache/dedup form


def _cache_key(
    gen: str,
    query: str | None,
    components: dict[str, str] | None,
    project: str,
    lifecycle: tuple[str, ...],
    k: int,
) -> str:
    # namespaced by generation: a value stored under gen G was computed against immutable schema G,
    # so a cutover never mis-serves. payload = project + lifecycle + k + normalized inputs
    q = normalize(query) if query else ""
    c = {f: normalize(v) for f, v in components.items()} if components else {}
    payload = json.dumps(
        [gen, project, list(lifecycle), k, q, c], sort_keys=True, ensure_ascii=False
    )
    return "address:" + hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()


async def _resolve_cached(
    gen: str,
    query: str | None,
    components: dict[str, str] | None,
    project: str,
    lifecycle: tuple[str, ...],
    limit: int,
    source: SourceSnapshot,
    rt: _Runtime,
    known_miss: bool = False,
) -> Resolution:
    # the entry holds the full ranking, so every limit under TOP_K shares one
    key = _cache_key(gen, query, components, project, lifecycle, rank_width(limit))
    resolution = source.resolution
    assert resolution is not None  # every /resolve source is built with a resolution_factory
    return await _cached(
        rt,
        key,
        lambda: resolve_request(
            query,
            components,
            project=project,
            lifecycle=lifecycle,
            limit=limit,
            source=source,
            geo_source=source,
            resolution=resolution,
        ),
        known_miss=known_miss,
    )


def _search_cache_key(
    gen: str, target: str, query: str, limit: int, lifecycle: tuple[str, ...]
) -> str:
    # token folds as search_one dispatches (codes raw, names folded) so codes never cross-collide
    token = search_cache_token(target, query, normalize)
    payload = json.dumps(
        [gen, target, token, limit, list(lifecycle)], sort_keys=True, ensure_ascii=False
    )
    return "search:" + hashlib.blake2b(payload.encode(), digest_size=16).hexdigest()


async def _search_cached(
    gen: str,
    query: str,
    target: str,
    limit: int,
    lifecycle: tuple[str, ...],
    source: SourceSnapshot,
    rt: _Runtime,
    known_miss: bool = False,
) -> Resolution:
    key = _search_cache_key(gen, target, query, limit, lifecycle)
    return await _cached(
        rt,
        key,
        lambda: search_request(query, target, geo_source=source, limit=limit, lifecycle=lifecycle),
        known_miss=known_miss,
    )


async def _run_batch[T](
    items: list[T],
    key: Callable[[T], Hashable],
    rt: _Runtime,
    cache_key: Callable[[T], str],
    run: Callable[[T, Resolution | None], Awaitable[dict[str, Any]]],
) -> list[dict[str, Any]]:
    # dedup identical items, then mget the distinct keys: hits map inline, only misses take the pool
    cache = rt.cache
    distinct: dict[Hashable, T] = {}
    for item in items:
        distinct.setdefault(key(item), item)
    distinct_items = list(distinct.items())
    hits = (
        await cache.mget([cache_key(it) for _, it in distinct_items])
        if cache
        else [None] * len(distinct_items)
    )

    async def handle(item: T, cached: Resolution | None) -> dict[str, Any]:
        if cached is not None:
            return await run(item, cached)
        async with rt.batch_sem:
            return await run(item, None)

    results = await asyncio.gather(
        *(handle(it, hit) for (_, it), hit in zip(distinct_items, hits, strict=True))
    )
    done = dict(zip((dk for dk, _ in distinct_items), results, strict=True))
    return [done[key(item)] for item in items]


async def _batch(
    gen: str,
    items: list[str | ResolveItem],
    source: SourceSnapshot,
    rt: _Runtime,
    *,
    geometry: bool,
    uuid: bool,
) -> list[dict[str, Any]]:
    norm = [it if isinstance(it, ResolveItem) else ResolveItem(input=it) for it in items]

    def key(it: ResolveItem) -> Hashable:
        # limit rides the dedup key though not the cache key: two limits of one query render
        # differently off the same entry, and the singleflight map still collapses their compute
        lc = _canon_lifecycle(it.lifecycle)
        return (it.input, it.project, lc, it.limit, tuple(sorted((it.components or {}).items())))

    def ckey(it: ResolveItem) -> str:
        return _cache_key(
            gen,
            it.input,
            it.components,
            it.project,
            _canon_lifecycle(it.lifecycle),
            rank_width(it.limit),
        )

    async def run(it: ResolveItem, cached: Resolution | None) -> dict[str, Any]:
        try:
            resolution = (
                cached
                if cached is not None
                else await _resolve_cached(
                    gen,
                    it.input,
                    it.components,
                    it.project,
                    _canon_lifecycle(it.lifecycle),
                    it.limit,
                    source,
                    rt,
                    known_miss=True,  # the batch mget already read this key
                )
            )
        except Exception as e:  # per-item isolation: a failed resolve never sinks the batch
            _log.warning("[!] batch resolve failed for %r: %s", it.input, e)
            return {"query": it.input or "", "error": "resolution failed"}
        # mapper stays outside the try so a wire-mapping bug surfaces as 500, not a 200 envelope
        return to_result(it.input or "", resolution, geometry=geometry, uuid=uuid, limit=it.limit)

    return await _run_batch(norm, key, rt, ckey, run)


async def _search_batch(
    gen: str,
    items: list[SearchItem],
    source: SourceSnapshot,
    rt: _Runtime,
    *,
    geometry: bool,
) -> list[dict[str, Any]]:
    def key(it: SearchItem) -> Hashable:
        return (it.input, it.target, it.limit, _canon_lifecycle(it.lifecycle))

    def ckey(it: SearchItem) -> str:
        return _search_cache_key(gen, it.target, it.input, it.limit, _canon_lifecycle(it.lifecycle))

    async def run(it: SearchItem, cached: Resolution | None) -> dict[str, Any]:
        try:
            resolution = (
                cached
                if cached is not None
                else await _search_cached(
                    gen,
                    it.input,
                    it.target,
                    it.limit,
                    _canon_lifecycle(it.lifecycle),
                    source,
                    rt,
                    known_miss=True,  # the batch mget already read this key
                )
            )
        except Exception as e:
            _log.warning("[!] batch search failed for %r: %s", it.input, e)
            return {"query": it.input, "error": "search failed"}
        return to_result(it.input, resolution, geometry=geometry, uuid=False, limit=it.limit)

    return await _run_batch(items, key, rt, ckey, run)


# response_model=None returns _JSONResponse straight (no jsonable_encoder walk); responses= keeps
# the 200 schema documented
_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "model": AddressResult | list[AddressResult | AddressError],
        "headers": {
            DATA_UPDATED_HEADER: {
                "description": "When the data behind this response was last updated (UTC).",
                "schema": {"type": "string", "format": "date-time"},
            }
        },
    }
}


@router.post(
    "/resolve",
    response_model=None,
    responses=_RESPONSES,
    summary="Resolve address input",
    description=(
        "Resolve free text or pinned components to ranked address matches, optionally "
        "re-expressed at a coarser layer via `project`. `limit` sets how many come back. "
        "Pass a list as `query` for a batch."
    ),
)
async def resolve_endpoint(req: AddressRequest, request: Request) -> Response:
    source = request.app.state.source
    if source is None:
        raise HTTPException(status_code=503, detail="warming up")
    rt = request.app.state.rt
    # pin one serving snapshot for the whole request: the query and its cache key share a generation
    async with source.snapshot() as snap:
        gen = snap.generation
        headers = data_updated(snap.seeded_at)  # captured here; the single path renders post-exit
        if isinstance(req.query, list):
            results = await _batch(gen, req.query, snap, rt, geometry=req.geometry, uuid=req.uuid)
            return _JSONResponse(results, headers=headers)
        resolution = await _resolve_cached(
            gen,
            req.query,
            req.components,
            req.project,
            _canon_lifecycle(req.lifecycle),
            req.limit,
            snap,
            rt,
        )
    return _JSONResponse(
        to_result(
            req.query or "", resolution, geometry=req.geometry, uuid=req.uuid, limit=req.limit
        ),
        headers=headers,
    )


@router.post(
    "/search",
    response_model=None,
    responses=_RESPONSES,
    summary="Look up one register",
    description=(
        "Match a name or a code against a single register named by `target`. No address "
        "parsing. Pass a list as `query` for a batch."
    ),
)
async def search_endpoint(req: SearchRequest, request: Request) -> Response:
    source = request.app.state.source
    if source is None:
        raise HTTPException(status_code=503, detail="warming up")
    rt = request.app.state.rt
    async with source.snapshot() as snap:
        gen = snap.generation
        headers = data_updated(snap.seeded_at)
        if isinstance(req.query, list):
            results = await _search_batch(gen, req.query, snap, rt, geometry=req.geometry)
            return _JSONResponse(results, headers=headers)
        assert req.target is not None  # the single-query validator rejects a missing target
        resolution = await _search_cached(
            gen, req.query, req.target, req.limit, _canon_lifecycle(req.lifecycle), snap, rt
        )
    return _JSONResponse(
        to_result(req.query, resolution, geometry=req.geometry, uuid=False, limit=req.limit),
        headers=headers,
    )


def create_app() -> FastAPI:
    settings = Settings()
    # per uvicorn worker (each process calls this factory): wire bifrost.* -> stdout;
    # uvicorn's disable_existing_loggers=False lets these propagate to the root handler
    logging.basicConfig(
        level=settings.log_level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        segmenter.load()  # warm; reads $BIFROST_SEGMENTER, fail-fast if artifact missing
        app.state.source = None
        cache = (
            await ResponseCache.connect(settings.cache_url, settings.cache_ttl)
            if settings.cache_url
            else None
        )
        app.state.rt = _Runtime(
            cache=cache,
            # half the pool, shared across batches: a big batch must not starve interactive requests
            batch_sem=asyncio.Semaphore(max(1, settings.db_pool_max // 2)),
            inflight={},
        )
        init_task = asyncio.create_task(_init_source(app, settings))
        try:
            yield
        finally:
            init_task.cancel()
            with suppress(asyncio.CancelledError):
                await init_task
            if app.state.rt.cache:
                await app.state.rt.cache.close()
            if app.state.source:
                await app.state.source.close()

    app = FastAPI(
        title="Bifrost",
        version=settings.version,
        description=(
            "Danish address resolution and register lookup. Coordinates are EPSG:25832. "
            f"Every successful response carries `{DATA_UPDATED_HEADER}`, the UTC time the "
            "data behind it was last updated."
        ),
        docs_url="/swagger",  # /docs is the documentation site's path on the shared host
        lifespan=lifespan,
        default_response_class=_JSONResponse,
    )
    app.add_middleware(  # innermost of the three: rejections still carry cors headers and metrics
        LimitsMiddleware,
        max_body_bytes=settings.max_body_bytes,
        max_inflight=settings.db_pool_max * 4,  # enough waiters to keep the pool busy, then shed
        request_timeout=settings.request_timeout,
    )
    # level 1, not starlette's default 9: 2.3x vs 2.7x on geojson at a 17th of the cpu
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        expose_headers=[DATA_UPDATED_HEADER],  # else browser js can't read it off the response
    )
    metrics.setup(app)
    app.include_router(router)
    return app


def run() -> None:
    import uvicorn

    settings = Settings()
    uvicorn.run(
        "bifrost.api.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=8000,
        workers=settings.workers,
        access_log=False,
    )


if __name__ == "__main__":
    run()
