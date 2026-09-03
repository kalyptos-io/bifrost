"""postgres AddressSource (asyncpg): pure fetch, zero scoring.

the graded street stream and husnr/locality recovery sets feed core.merge. trigram-KNN ranking and
per-street display/similarity come from an in-process StreetIndex; only fact rows hit postgres.

serving state (pool + both indexes + generation) is one refcounted `_Serving` snapshot. a request
calls snapshot() once at entry and reads everything through that pinned view, so a mid-request
cutover never mixes two generations' street_id spaces and the response-cache key matches the
generation the query actually ran against. the refresh loop polls public.generations and, on a
newer matching generation, builds a fresh pool bound to its schema, swaps `_serving`, then retires
the old one - its pool closes only once the last in-flight snapshot releases it. each worker also
heartbeats a serving lease so gc never drops a schema it is still resolving; a failing beat keeps
serving and counts, never gates readiness - the write it fails on and the gc it guards against both
need the same primary, so a beat can only fail while the drop it prevents is impossible.
"""

import asyncio
import contextlib
import logging
import os
import random
import socket
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import datetime
from typing import NamedTuple

import asyncpg
from prometheus_client import Counter

from bifrost.arms.area_index import AreaHit, AreaIndex
from bifrost.arms.aux_index import AuxMaps
from bifrost.arms.stednavne_index import StednavneIndex
from bifrost.arms.street_index import StreetIndex
from bifrost.core.context import ResolutionContext
from bifrost.core.types import (
    AREA_PROJECTION_TARGETS,
    CURRENT_LIFECYCLE,
    AddressRow,
    AreaGeom,
    EjendomGeom,
    EjendomType,
    PropertyRef,
    RoadGeom,
    StednavnGeom,
)
from bifrost.db import generations
from bifrost.db.contracts import CURRENT

_log = logging.getLogger(__name__)

_HOLDER = f"{socket.gethostname()}:{os.getpid()}"  # per-worker serving-lease identity (pod + pid)
_LEASE_FAILURES = Counter(
    "bifrost_serving_lease_failures", "serving-lease heartbeats this worker could not write"
)


class NotSeeded(Exception):
    """no generation matches the build fingerprint yet; the source can't be built (transient)."""


# addresses-only facts; street_dim cols (display + similarity) are joined or carried per query
_ADDR_FACTS = (
    "a.address_id, a.street_id, a.house_number, a.house_letter, a.floor, a.door, "
    "a.postcode, a.sub_locality, a.city, a.adgangspunkt_x, a.adgangspunkt_y, a.vejpunkt_x, "
    "a.vejpunkt_y"
)
_FACTS = f"{_ADDR_FACTS}, d.street, d.folded_street"
_SIM = "similarity(d.folded_street, $1) AS street_similarity"
_PROJECTION = f"{_FACTS}, a.lifecycle, {_SIM}"
_ROW_SELECT = f"SELECT {_PROJECTION} FROM addresses a JOIN street_dim d USING (street_id)"
# carry sim + display from the combo (constant per street); avoids re-joining street_dim per row.
# alias combos are lifecycle-filtered in-proc (present all their addresses); canonical filter on $6
_STREAM_ROWS = (
    f"SELECT {_ADDR_FACTS}, c.street, c.folded_street, c.sim AS street_similarity, "
    "coalesce(c.alias_lifecycle, a.lifecycle) AS lifecycle "
    "FROM unnest($1::int[], $2::text[], $3::float8[], $4::text[], $5::text[], $7::text[]) "
    "AS c(street_id, postcode, sim, street, folded_street, alias_lifecycle) "
    "JOIN addresses a USING (street_id, postcode) "
    "WHERE c.alias_lifecycle IS NOT NULL OR a.lifecycle = ANY($6::text[])"
)
# drop units a base (floor/door-null) row already represents; base-less groups stream intact.
# not recall-neutral: can flip a score-tie among same-husnr lettered addrs (~0.02%).
_STREAM_ROWS_COLLAPSED = (
    f"WITH s AS MATERIALIZED ({_STREAM_ROWS}) SELECT * FROM s r "
    "WHERE (r.floor IS NULL AND r.door IS NULL) OR NOT EXISTS ("
    "SELECT 1 FROM s b WHERE b.street_id = r.street_id AND b.postcode = r.postcode "
    "AND b.house_number = r.house_number "
    "AND coalesce(b.house_letter, '') = coalesce(r.house_letter, '') "
    "AND b.floor IS NULL AND b.door IS NULL)"
)
# husnr recovery: only husnr-match rows answer (differing-length grades 0). matches off composite
# index; canonical designation, so it presents + filters on the fact row's own lifecycle.
_HUSNR_HITS = (
    f"SELECT {_ADDR_FACTS}, a.lifecycle FROM addresses a "
    "WHERE a.postcode = ANY($1) AND a.house_number = $2 AND a.lifecycle = ANY($4) LIMIT $3"
)
# geo geometry fetches: only the <=k hits' geojson, by id (the in-proc indexes hold no geometry).
# roads for the matched streets; postcodes && filters to a pin (null = no pin)
_ROAD_GEOM = (
    "SELECT navngivenvej_id, street_id, postcodes, geometry, lifecycle FROM road "
    "WHERE street_id = ANY($1::int[]) AND ($2::text[] IS NULL OR postcodes && $2::text[])"
)
_AREA_GEOM = "SELECT area_id, geometry FROM admin_area WHERE area_id = ANY($1)"
_STEDNAVNE_GEOM = "SELECT stednavn_id, geometry FROM stednavne WHERE stednavn_id = ANY($1)"
# uniform property card: the property row (e), its ground sfe row (g, for the merged footprint) and
# the matched parcel (m). geometry = ground footprint (merged for multi-parcel sfes, else parcel)
_EJENDOM_CARD = (
    "e.bfe, e.type, e.ejerlejlighedsnummer, e.ground_bfe, "
    "e.chain_bfes, e.chain_types, e.children_bfes, e.children_types, "
    "m.jordstykke, m.matrikelnummer, m.ejerlavskode, m.ejerlavsnavn, "
    "m.kommunekode, m.kommunenavn, m.centroid, m.matrikelbetegnelse, "
    "coalesce(g.geometry, m.geometry) AS geometry"
)
# digit query hits bfe (pk probe) or ejerlavskode (its in-ejerlav ground properties, one parcel per
# bfe); probe wins on a bfe clash, ejerlav rows carry their own in-ejerlav parcel as the match
_EJENDOM_BY_CODE = (
    "WITH probe AS ("
    "SELECT bfe, NULL::text AS ctx, 1 AS pri FROM ejendom WHERE bfe = $1 "
    "UNION ALL "
    "SELECT bfe, jordstykke AS ctx, 2 AS pri FROM ("
    "SELECT DISTINCT ON (bfe) bfe, jordstykke FROM matrikel "
    "WHERE ejerlavskode = $1 ORDER BY bfe, jordstykke) ej"
    "), dedup AS (SELECT DISTINCT ON (bfe) bfe, ctx, pri FROM probe ORDER BY bfe, pri) "
    f"SELECT {_EJENDOM_CARD}, e.lifecycle AS lifecycle FROM dedup d "
    "JOIN ejendom e ON e.bfe = d.bfe "
    "LEFT JOIN ejendom g ON g.bfe = e.ground_bfe "
    "LEFT JOIN matrikel m ON m.jordstykke = coalesce(d.ctx, g.jordstykke) "
    "WHERE e.lifecycle = ANY($3) "
    "ORDER BY d.pri, e.bfe LIMIT $2"
)
# betegnelse lives on parcels; bounded knn overfetch min(10*limit, 1000) keeps gist order so a
# property drowned by >10x similar sibling parcels can still surface before the per-bfe dedup.
# current-only path: filters the ejendom's own lifecycle (not the matched parcel's)
_EJENDOM_BY_BETEGNELSE = (
    "WITH knn AS ("
    "SELECT bfe, jordstykke, word_similarity($1, folded_betegnelse) AS sim FROM matrikel "
    "WHERE $1 <% folded_betegnelse "
    "ORDER BY $1 <<-> folded_betegnelse LIMIT least($2 * 10, 1000)"
    "), best AS (SELECT DISTINCT ON (bfe) bfe, jordstykke, sim FROM knn ORDER BY bfe, sim DESC) "
    f"SELECT {_EJENDOM_CARD}, e.lifecycle AS lifecycle, b.sim AS sim FROM best b "
    "JOIN ejendom e ON e.bfe = b.bfe "
    "LEFT JOIN ejendom g ON g.bfe = e.ground_bfe "
    "LEFT JOIN matrikel m ON m.jordstykke = b.jordstykke "
    "WHERE e.lifecycle = ANY($3) "
    "ORDER BY b.sim DESC, e.bfe LIMIT $2"
)
# history path (non-current lifecycles requested): matrikel retains its non-current parcels so the
# betegnelse KNN scans them directly (gsearch matrikel_udgaaet parity). a row qualifies on the
# parcel or ejendom lifecycle, filtered pre-dedup so a qualifying parcel can't be hidden by a
# higher-sim canonical one; a non-current parcel sets the presented lifecycle, jordstykke tiebreaks
_EJENDOM_BY_BETEGNELSE_HIST = (
    "WITH knn AS ("
    "SELECT bfe, jordstykke, lifecycle AS parcel_lc, "
    "word_similarity($1, folded_betegnelse) AS sim FROM matrikel "
    "WHERE $1 <% folded_betegnelse "
    "ORDER BY $1 <<-> folded_betegnelse LIMIT least($2 * 10, 1000)"
    "), qualified AS ("
    "SELECT k.bfe, k.jordstykke, k.sim, "
    "CASE WHEN k.parcel_lc <> 'current' AND k.parcel_lc = ANY($3) "
    "THEN k.parcel_lc ELSE e.lifecycle END AS lifecycle "
    "FROM knn k JOIN ejendom e ON e.bfe = k.bfe "
    "WHERE (k.parcel_lc <> 'current' AND k.parcel_lc = ANY($3)) OR e.lifecycle = ANY($3)"
    "), best AS ("
    "SELECT DISTINCT ON (bfe) bfe, jordstykke, sim, lifecycle FROM qualified "
    "ORDER BY bfe, sim DESC, jordstykke) "
    f"SELECT {_EJENDOM_CARD}, b.lifecycle AS lifecycle, b.sim AS sim "
    "FROM best b "
    "JOIN ejendom e ON e.bfe = b.bfe "
    "LEFT JOIN ejendom g ON g.bfe = e.ground_bfe "
    "LEFT JOIN matrikel m ON m.jordstykke = b.jordstykke "
    "ORDER BY b.sim DESC, e.bfe LIMIT $2"
)
# projection fan-out: specific bfes; the paired context jordstykke (the address's own parcel)
# splices in place of the representative, else falls back to the ground representative
_EJENDOM_BY_BFES = (
    f"SELECT {_EJENDOM_CARD}, e.lifecycle AS lifecycle "
    "FROM unnest($1::text[], $2::text[]) AS r(bfe, ctx) "
    "JOIN ejendom e ON e.bfe = r.bfe "
    "LEFT JOIN ejendom g ON g.bfe = e.ground_bfe "
    "LEFT JOIN matrikel m ON m.jordstykke = coalesce(r.ctx, g.jordstykke)"
)
# projection: the denormalized column of one kind for the resolved hits (by pk, off the stream).
# ejendom's ejendom_bfe (the dedup key) + jordstykke (the card's parcel context) ride here too.
_AREA_CODE_COLUMN = {
    "kommune": "kommunekode",
    "region": "regionskode",
    "sogn": "sognekode",
    "retskreds": "retskredsnummer",
    "politikreds": "politikredsnummer",
    "opstillingskreds": "opstillingskredsnummer",
    "ejendom": "ejendom_bfe",
    "jordstykke": "jordstykke",
}
# every projectable area kind has a column (ejendom/jordstykke are extras); a missing one empties it
assert set(_AREA_CODE_COLUMN) >= AREA_PROJECTION_TARGETS


def _make_row(rec: asyncpg.Record, *, street: str, folded_street: str, sim: float) -> AddressRow:
    return AddressRow(
        address_id=rec["address_id"],
        street_id=rec["street_id"],
        street=street,
        folded_street=folded_street,
        house_number=rec["house_number"],
        house_letter=rec["house_letter"],
        floor=rec["floor"],
        door=rec["door"],
        postcode=rec["postcode"],
        sub_locality=rec["sub_locality"],
        street_similarity=sim,
        adgangspunkt_x=rec["adgangspunkt_x"],
        adgangspunkt_y=rec["adgangspunkt_y"],
        vejpunkt_x=rec["vejpunkt_x"],
        vejpunkt_y=rec["vejpunkt_y"],
        city=rec["city"],
        lifecycle=rec["lifecycle"],
    )


def _row(rec: asyncpg.Record) -> AddressRow:
    return _make_row(
        rec, street=rec["street"], folded_street=rec["folded_street"], sim=rec["street_similarity"]
    )


def _refs(bfes: list[str], types: list[str]) -> tuple[PropertyRef, ...]:
    # strict: parallel arrays out of sync is corruption, not something to silently truncate
    return tuple(PropertyRef(b, EjendomType(t)) for b, t in zip(bfes, types, strict=True))


def _ejendom_geom(rec: asyncpg.Record, sim: float) -> EjendomGeom:
    return EjendomGeom(
        rec["bfe"],
        EjendomType(rec["type"]),
        rec["ejerlejlighedsnummer"],
        _refs(rec["chain_bfes"], rec["chain_types"]),
        rec["ground_bfe"] is not None,  # chain_complete: a set ground = the chain reaches an sfe
        _refs(rec["children_bfes"], rec["children_types"]),
        rec["jordstykke"],
        rec["matrikelnummer"],
        rec["ejerlavskode"],
        rec["ejerlavsnavn"],
        rec["kommunekode"],
        rec["kommunenavn"],
        rec["centroid"],
        rec["matrikelbetegnelse"],
        sim,
        rec["geometry"],
        rec["lifecycle"],
    )


_REFRESH_INTERVAL = 300.0  # secs between generation-registry polls for an atomic cutover
_COMMAND_TIMEOUT = 30.0  # secs: cap a hung query so it can't pin a pool slot indefinitely
_LEASE_TIMEOUT = 10.0  # secs: a lease beat must never stall the refresh tick behind it


class _Serving:
    """the atomic serving snapshot: a pool bound to one generation + its in-proc indexes. refcounted
    so a retired (post-cutover) pool closes only once the last in-flight snapshot releases it."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        index: StreetIndex,
        area_index: AreaIndex,
        stednavne_index: StednavneIndex,
        resolution: ResolutionContext | None,  # per-gen branches + merge ctx; None for geo-only
        generation: str,  # schema_name; namespaces cache keys, detects a newer generation
        contract_version: int,  # the serving generation's contract; gates no-downgrade cutovers
        seeded_at: datetime,  # db-clock load time of this generation; served as a freshness header
    ) -> None:
        self.pool = pool
        self.index = index
        self.area_index = area_index
        self.stednavne_index = stednavne_index
        self.resolution = resolution
        self.generation = generation
        self.contract_version = contract_version
        self.seeded_at = seeded_at
        self._refs = 0
        self._retired = False

    def acquire(self) -> None:
        self._refs += 1  # GIL-atomic; one event loop, no true concurrency on the count

    async def release(self) -> None:
        self._refs -= 1
        if self._retired and self._refs == 0:
            await self._close()

    async def retire(self) -> None:
        # superseded by cutover (or shutdown): close now if idle, else the last release closes it
        self._retired = True
        if self._refs == 0:
            await self._close()

    async def _close(self) -> None:
        with contextlib.suppress(Exception):
            await self.pool.close()


class _ConnParams(NamedTuple):
    dsn: str
    host: str | None
    min_size: int
    max_size: int


async def _create_pool(p: _ConnParams, schema: str) -> asyncpg.Pool:
    # the generations row commits last, so a backend that sees it replayed the whole generation
    async def _replayed(conn: asyncpg.Connection) -> None:
        if not await conn.fetchval(
            "SELECT 1 FROM public.generations WHERE schema_name = $1", schema
        ):
            raise RuntimeError(f"generation {schema} not replayed on this backend")

    # bind the pool to the generation schema; serving SQL stays unqualified, public is the fallback
    pool = asyncpg.create_pool(
        p.dsn,
        host=p.host,
        min_size=p.min_size,
        max_size=p.max_size,
        command_timeout=_COMMAND_TIMEOUT,
        server_settings={"search_path": f"{schema}, public"},
        init=_replayed,
    )
    try:
        return await pool
    except BaseException:  # a sibling min_size connection may already be open
        pool.terminate()
        raise


class SourceSnapshot:
    """one request's pinned view over a single `_Serving`: implements AddressSource + GeoSource. an
    async context manager - it holds a ref for its lifetime so a retired pool can't close under it,
    and releases on exit; every read goes through this one pinned serving object."""

    def __init__(self, serving: _Serving) -> None:
        self._serving = serving
        serving.acquire()

    async def __aenter__(self) -> "SourceSnapshot":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._serving.release()

    @property
    def generation(self) -> str:
        return self._serving.generation

    @property
    def contract_version(self) -> int:
        return self._serving.contract_version

    @property
    def seeded_at(self) -> datetime:
        return self._serving.seeded_at

    @property
    def resolution(self) -> ResolutionContext | None:
        return self._serving.resolution

    async def street_stream(
        self,
        folded_q: str,
        *,
        cap: int,
        batch: int,
        collapse_units: bool = False,
        postcodes: set[str] | None = None,
        lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
    ) -> AsyncGenerator[list[AddressRow]]:
        srv = self._serving
        combos = srv.index.knn(folded_q, cap=cap, postcodes=postcodes, lifecycle=lifecycle)
        if not combos:
            return
        rows_sql = _STREAM_ROWS_COLLAPSED if collapse_units else _STREAM_ROWS
        lc = list(lifecycle)
        # fetch per batch, not one held conn: the pool slot is freed while the consumer scores
        for i in range(0, len(combos), batch):
            chunk = combos[i : i + batch]
            recs = await srv.pool.fetch(
                rows_sql,
                [c.street_id for c in chunk],
                [c.postcode for c in chunk],
                [c.sim for c in chunk],
                [c.street for c in chunk],
                [c.folded_street for c in chunk],
                lc,
                [c.alias_lifecycle for c in chunk],
            )
            yield [_row(r) for r in recs]

    async def by_postcodes(
        self,
        codes: set[str],
        folded_q: str | None,
        house_number: str | None,
        *,
        cap: int,
        lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
    ) -> list[AddressRow]:
        if not codes:
            return []
        srv = self._serving
        pcs = list(codes)
        lc = list(lifecycle)
        # bare locality: order keys constant, so skip the sort; planner stops the scan at the cap
        if not folded_q and house_number is None:
            sql = f"{_ROW_SELECT} WHERE a.postcode = ANY($2) AND a.lifecycle = ANY($4) LIMIT $3"
            recs = await srv.pool.fetch(sql, "", pcs, cap, lc)
            return [_row(r) for r in recs]
        # husnr present: composite-index matches, index resolves the distinct streets, stitched
        if house_number is not None:
            hits = await srv.pool.fetch(_HUSNR_HITS, pcs, house_number, cap, lc)
            if not hits:
                return []
            sids = list({h["street_id"] for h in hits})
            dims = srv.index.dims(sids, folded_q or "")
            return [
                _make_row(h, street=d.street, folded_street=d.folded_street, sim=d.sim)
                for h in hits
                if (d := dims.get(h["street_id"])) is not None
            ]
        # street, no husnr: similarity is the only selector for which rows survive the cap.
        sql = (
            f"{_ROW_SELECT} WHERE a.postcode = ANY($2) AND a.lifecycle = ANY($4) "
            "ORDER BY street_similarity DESC LIMIT $3"
        )
        recs = await srv.pool.fetch(sql, folded_q, pcs, cap, lc)
        return [_row(r) for r in recs]

    # --- GeoSource: street linestring + dagi area polygons, geometry fetched per <=k hit ---

    async def _geom_map(self, pool: asyncpg.Pool, sql: str, ids: list) -> dict:
        recs = await pool.fetch(sql, ids)
        return {r[0]: r[1] for r in recs}

    async def street_features(
        self,
        folded_street: str,
        *,
        cap: int,
        postcodes: set[str] | None = None,
        lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
    ) -> list[RoadGeom]:
        srv = self._serving
        streets = srv.index.rank_streets(folded_street, cap=cap, lifecycle=lifecycle)
        if not streets:
            return []
        pcs = list(postcodes) if postcodes else None
        recs = await srv.pool.fetch(_ROAD_GEOM, list({s.street_id for s in streets}), pcs)
        by_sid: dict[int, list[asyncpg.Record]] = {}
        for r in recs:
            by_sid.setdefault(r["street_id"], []).append(r)
        lc = set(lifecycle)
        # one feature per (designation, physical road), ranked by street sim; cap counts roads.
        # alias match keeps all rows at its designation lifecycle (name-history); canonical filters
        # each road on its own lifecycle so a retired road never leaks as current
        pairs = [
            (s, r)
            for s in streets
            for r in by_sid.get(s.street_id, ())
            if s.alias_lifecycle is not None or r["lifecycle"] in lc
        ]
        pairs.sort(key=lambda sr: (-sr[0].sim, sr[1]["navngivenvej_id"]))
        return [
            RoadGeom(
                s.street,
                s.sim,
                r["geometry"],
                tuple(r["postcodes"]),
                s.alias_lifecycle if s.alias_lifecycle is not None else r["lifecycle"],
            )
            for s, r in pairs[:cap]
        ]

    async def _areas(self, pool: asyncpg.Pool, hits: list[AreaHit]) -> list[AreaGeom]:
        if not hits:
            return []
        geoms = await self._geom_map(pool, _AREA_GEOM, [h.area_id for h in hits])
        return [AreaGeom(h.code, h.name, h.sim, geoms.get(h.area_id), h.lifecycle) for h in hits]

    async def area_by_code(
        self, kind: str, code: str, *, cap: int, lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE
    ) -> list[AreaGeom]:
        srv = self._serving
        lc = set(lifecycle)  # exact code is authoritative; lifecycle-filter its hits before the cap
        hits = [h for h in srv.area_index.by_code(code, kind=kind) if h.lifecycle in lc][:cap]
        return await self._areas(srv.pool, hits)

    async def area_by_name(
        self,
        kind: str,
        folded_name: str,
        *,
        cap: int,
        lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
    ) -> list[AreaGeom]:
        srv = self._serving
        hits = srv.area_index.knn(folded_name, kind=kind, cap=cap, lifecycle=lifecycle)
        return await self._areas(srv.pool, hits)

    async def ejendom_by_code(
        self, code: str, *, cap: int, lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE
    ) -> list[EjendomGeom]:
        # exact bfe/ejerlavskode lookup; pk probes over the ~2.7m-row table, straight from pg
        recs = await self._serving.pool.fetch(_EJENDOM_BY_CODE, code, cap, list(lifecycle))
        return [_ejendom_geom(r, 1.0) for r in recs]

    async def ejendom_by_betegnelse(
        self, folded_name: str, *, cap: int, lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE
    ) -> list[EjendomGeom]:
        # gist-accelerated word_similarity KNN over folded_betegnelse, grouped to one ground sfe;
        # the history path also surfaces non-current parcel designations on a non-current request
        sql = (
            _EJENDOM_BY_BETEGNELSE_HIST if set(lifecycle) - {"current"} else _EJENDOM_BY_BETEGNELSE
        )
        recs = await self._serving.pool.fetch(sql, folded_name, cap, list(lifecycle))
        return [_ejendom_geom(r, r["sim"]) for r in recs]

    async def stednavne_by_name(
        self, folded_name: str, *, cap: int, lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE
    ) -> list[StednavnGeom]:
        # in-proc trigram KNN over the whole register, then one geometry fetch by the <=k hits' pk
        srv = self._serving
        hits = srv.stednavne_index.knn(folded_name, cap=cap, lifecycle=lifecycle)
        if not hits:
            return []
        geoms = await self._geom_map(srv.pool, _STEDNAVNE_GEOM, [h.stednavn_id for h in hits])
        return [
            StednavnGeom(h.name, h.type, h.sim, geoms.get(h.stednavn_id), h.lifecycle) for h in hits
        ]

    async def areas_by_codes(self, kind: str, codes: list[str]) -> dict[str, AreaGeom]:
        srv = self._serving
        # projection fan-out: one in-proc index hit per code, then a single geometry fetch by id
        best = {
            code: hits[0] for code in codes if (hits := srv.area_index.by_code(code, kind=kind))
        }
        if not best:
            return {}
        geoms = await self._geom_map(srv.pool, _AREA_GEOM, [h.area_id for h in best.values()])
        return {c: AreaGeom(h.code, h.name, h.sim, geoms.get(h.area_id)) for c, h in best.items()}

    async def areas_by_names(self, kind: str, folded_names: list[str]) -> dict[str, AreaGeom]:
        srv = self._serving
        # name-keyed fan-out (city): one in-proc knn per name, then a single batched geometry fetch
        best = {
            name: hits[0]
            for name in folded_names
            if (hits := srv.area_index.knn(name, kind=kind, cap=1))
        }
        if not best:
            return {}
        geoms = await self._geom_map(srv.pool, _AREA_GEOM, [h.area_id for h in best.values()])
        return {n: AreaGeom(h.code, h.name, h.sim, geoms.get(h.area_id)) for n, h in best.items()}

    async def ejendom_by_bfes(self, refs: list[tuple[str, str | None]]) -> dict[str, EjendomGeom]:
        if not refs:
            return {}
        bfes = [b for b, _ in refs]
        ctxs = [c for _, c in refs]
        recs = await self._serving.pool.fetch(_EJENDOM_BY_BFES, bfes, ctxs)
        return {r["bfe"]: _ejendom_geom(r, 1.0) for r in recs}

    async def streets_by_names(
        self, pairs: list[tuple[str, set[str] | None]]
    ) -> dict[str, RoadGeom]:
        srv = self._serving
        # rank in-proc, one geometry fetch; pin filtered in-proc since pins differ per name
        ranked = {name: r[0] for name, _ in pairs if (r := srv.index.rank_streets(name, cap=1))}
        if not ranked:
            return {}
        roads: dict[int, list[asyncpg.Record]] = {}
        recs = await srv.pool.fetch(_ROAD_GEOM, [s.street_id for s in ranked.values()], None)
        for rec in recs:
            roads.setdefault(rec["street_id"], []).append(rec)
        out: dict[str, RoadGeom] = {}
        for name, pin in pairs:
            s = ranked.get(name)
            if s is None:
                continue
            # same road-lifecycle rule as street_features: a retired road never backs a projection
            hits = [
                r
                for r in roads.get(s.street_id, ())
                if (s.alias_lifecycle is not None or r["lifecycle"] in CURRENT_LIFECYCLE)
                and (pin is None or set(r["postcodes"]) & pin)
            ]
            if hits:
                best = min(hits, key=lambda r: r["navngivenvej_id"])  # sim const per street
                out[name] = RoadGeom(s.street, s.sim, best["geometry"], tuple(best["postcodes"]))
        return out

    async def address_area_codes(self, address_ids: list[str], kind: str) -> dict[str, str]:
        col = _AREA_CODE_COLUMN.get(kind)  # whitelisted: col interpolated, address_ids parametrized
        if not col or not address_ids:
            return {}
        sql = (
            f"SELECT address_id, {col} FROM addresses "
            f"WHERE address_id = ANY($1) AND {col} IS NOT NULL"
        )
        recs = await self._serving.pool.fetch(sql, address_ids)
        return {r["address_id"]: r[col] for r in recs}


class PostgresAddressSource:
    """lifecycle for the serving snapshot: opens/refreshes/retires the generation-bound pool and
    indexes, and heartbeats its serving lease. hand out snapshot() per request for the reads."""

    def __init__(self, serving: _Serving) -> None:
        self._serving = serving  # single reference; reassignment is atomic under the GIL
        self._params: _ConnParams | None = None  # set by connect(); rebuilds pools on cutover
        self._refresh_task: asyncio.Task | None = None
        # set by connect(); _tick rebuilds the resolution off the new pool so aux follows the data
        self._resolution_factory: Callable[[AuxMaps], ResolutionContext] | None = None

    @property
    def generation(self) -> str:
        return self._serving.generation  # the serving generation's schema_name (cache namespace)

    @contextlib.asynccontextmanager
    async def _lease_conn(self) -> AsyncIterator[asyncpg.Connection | None]:
        """a connection to the dsn's own host for the one thing this app writes.

        never the serving pool: database_host may aim that at a read-replica service, where the
        lease insert dies on a read-only transaction. one connect per beat beats owning a pool that
        would idle 300s between writes and go stale across a failover anyway."""
        if self._params is None:  # caller-owned pool (from_pool): no dsn to reach the primary by
            yield None
            return
        conn = await asyncpg.connect(
            self._params.dsn, timeout=_LEASE_TIMEOUT, command_timeout=_LEASE_TIMEOUT
        )
        try:
            yield conn
        finally:
            await conn.close()

    async def _beat(self, schema_name: str) -> None:
        # keep serving through a lease failure, but count it: gc leaves a schema alone for an hour
        # after the last beat, so a run of these is an alert, never a reason to leave the rotation
        try:
            async with self._lease_conn() as conn:
                if conn is not None:
                    await generations.heartbeat(conn, _HOLDER, schema_name)
        except Exception as e:
            _LEASE_FAILURES.inc()
            _log.warning("[!] serving-lease heartbeat failed for %s: %s", schema_name, e)

    def snapshot(self) -> SourceSnapshot:
        return SourceSnapshot(self._serving)  # pin the current serving atomically for one request

    @classmethod
    async def connect(
        cls,
        dsn: str,
        *,
        host: str | None = None,
        min_size: int = 2,
        max_size: int = 10,
        refresh_interval: float | None = _REFRESH_INTERVAL,
        resolution_factory: Callable[[AuxMaps], ResolutionContext] | None = None,
    ) -> "PostgresAddressSource":
        # find the current generation before opening the serving pool (the pool's search_path is
        # baked at creation and can't change); a shape-bumped image validly precedes its generation
        conn = await asyncpg.connect(dsn, host=host)
        try:
            gen = await generations.select_current(conn)
        finally:
            await conn.close()
        if gen is None:
            raise NotSeeded
        params = _ConnParams(dsn, host, min_size, max_size)
        pool = await _create_pool(params, gen.schema_name)
        try:
            self = await cls.from_pool(
                pool,
                generation=gen.schema_name,
                contract_version=gen.contract_version,
                seeded_at=gen.seeded_at,
                resolution_factory=resolution_factory,
            )
        except BaseException:
            await pool.close()  # load_from raised; don't orphan the pool we just opened
            raise
        self._params = params
        self._resolution_factory = resolution_factory  # _tick rebuilds the resolution on cutover
        await self._beat(gen.schema_name)  # first lease; the refresh loop keeps it warm
        if refresh_interval is not None:
            self._refresh_task = asyncio.create_task(self._refresh_loop(refresh_interval))
        return self

    @classmethod
    async def from_pool(
        cls,
        pool: asyncpg.Pool,
        *,
        generation: str,
        seeded_at: datetime,
        contract_version: int = CURRENT.version,
        resolution_factory: Callable[[AuxMaps], ResolutionContext] | None = None,
    ) -> "PostgresAddressSource":
        # builds the serving snapshot over a caller-owned pool; no refresh loop (it can't rebuild a
        # pool without the conn params) - connect() owns cutover, direct callers are one-shot
        index = await StreetIndex.load_from(pool)
        area_index = await AreaIndex.load_from(pool)
        stednavne_index = await StednavneIndex.load_from(pool)
        resolution = None
        if resolution_factory is not None:  # geo-only callers skip aux; no branches, no merge ctx
            resolution = resolution_factory(await AuxMaps.load_from(pool))
        return cls(
            _Serving(
                pool,
                index,
                area_index,
                stednavne_index,
                resolution,
                generation,
                contract_version,
                seeded_at,
            )
        )

    async def _refresh_loop(self, interval: float) -> None:
        await asyncio.sleep(random.uniform(0, interval))  # stagger worker cutovers off lockstep
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log.warning("[!] generation cutover failed: %s", e)
            await asyncio.sleep(interval)

    async def _tick(self) -> None:
        # heartbeat first (outside the cutover path) so a pod stuck failing cutover still holds its
        # lease and gc won't drop the schema under it; then build a fresh pool + indexes and swap
        s = self._serving
        await self._beat(s.generation)
        gen = await generations.select_current(s.pool)
        if (
            gen is None
            or self._params is None
            or not generations.should_swap(s.generation, s.contract_version, gen)
        ):
            return
        new_pool = await _create_pool(self._params, gen.schema_name)
        try:
            index = await StreetIndex.load_from(new_pool)
            area_index = await AreaIndex.load_from(new_pool)
            stednavne_index = await StednavneIndex.load_from(new_pool)
            resolution = None
            if self._resolution_factory is not None:  # aux follows the address data on cutover
                resolution = self._resolution_factory(await AuxMaps.load_from(new_pool))
        except BaseException:
            await new_pool.close()  # don't orphan a pool whose index build failed
            raise
        self._serving = _Serving(
            new_pool,
            index,
            area_index,
            stednavne_index,
            resolution,
            gen.schema_name,
            gen.contract_version,
            gen.seeded_at,
        )
        _log.info("[+] cut over to generation %s", gen.schema_name)
        await s.retire()  # close the old pool once its in-flight snapshots drain

    async def close(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
        with contextlib.suppress(Exception):
            async with self._lease_conn() as conn:
                if conn is not None:
                    await generations.drop_lease(conn, _HOLDER)
        await self._serving.retire()
