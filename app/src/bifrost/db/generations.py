"""dataset generations: each seed lands in its own gen_<ts> schema holding all 8 data tables; the
app selects the newest generation of a supported contract (current preferred, previous as fallback)
and cuts over atomically. the completed row's INSERT into public.generations IS the cutover - no
separate pointer flip.

pure helpers (Generation, gc_targets, schema-name codec) carry no I/O so the tests drive them; the
conn-taking helpers reach public.* explicitly (a gen schema never holds generations).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg

from bifrost.db.contracts import CURRENT, PREVIOUS, Contract

_SCHEMA_PREFIX = "gen_"
_TS_FORMAT = "%Y%m%d%H%M%S%f"  # sortable, all-digit -> safe as a bare pg identifier suffix
_KEEP_CURRENT = 2  # newest current-contract generations a cutting-over pod may still serve
_KEEP_PREVIOUS = 1  # newest previous-contract generation retained for rollback
_KEEP_RETIRED = 1  # newest generation of a retired pair stays a rollback target
_GC_GRACE = timedelta(hours=1)  # drop only past this, so no live pool still resolves the schema

GENERATIONS_DDL = """
CREATE TABLE IF NOT EXISTS public.generations (
    schema_name      text        PRIMARY KEY,
    shape            text        NOT NULL,
    row_count        bigint      NOT NULL,
    area_count       bigint      NOT NULL,
    matrikel_count   bigint      NOT NULL,
    stednavne_count  bigint      NOT NULL,
    ejendom_count    bigint      NOT NULL DEFAULT 0,
    contract_version integer     NOT NULL DEFAULT 1,
    seeded_at        timestamptz NOT NULL DEFAULT now()
);
"""

# a serving worker heartbeats the schema it currently resolves; gc consults it so a schema a live
# (or recently-live) pod still serves is never dropped, even one stuck failing a cutover past grace.
# created by register() under sync's privileged role - the app role only needs INSERT/UPDATE.
LEASES_DDL = """
CREATE TABLE IF NOT EXISTS public.serving_leases (
    holder      text        PRIMARY KEY,
    schema_name text        NOT NULL,
    heartbeat   timestamptz NOT NULL DEFAULT now()
)
"""

_SELECT_COLS = (
    "schema_name, shape, row_count, area_count, matrikel_count, "
    "stednavne_count, ejendom_count, contract_version, seeded_at"
)
_INSERT_COLS = (
    "schema_name, shape, row_count, area_count, matrikel_count, "
    "stednavne_count, ejendom_count, contract_version"
)


@dataclass(frozen=True, slots=True)
class Generation:
    schema_name: str
    shape: str
    row_count: int
    area_count: int
    matrikel_count: int
    stednavne_count: int
    ejendom_count: int
    seeded_at: datetime
    contract_version: int = CURRENT.version


def _supported(current: Contract, previous: Contract | None) -> tuple[Contract, ...]:
    return (current,) if previous is None else (current, previous)


def schema_name_for(ts: datetime) -> str:
    return _SCHEMA_PREFIX + ts.astimezone(UTC).strftime(_TS_FORMAT)


def _schema_ts(name: str) -> datetime | None:
    if not name.startswith(_SCHEMA_PREFIX):
        return None
    try:
        return datetime.strptime(name[len(_SCHEMA_PREFIX) :], _TS_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _row_to_gen(r: asyncpg.Record) -> Generation:
    return Generation(
        r["schema_name"],
        r["shape"],
        r["row_count"],
        r["area_count"],
        r["matrikel_count"],
        r["stednavne_count"],
        r["ejendom_count"],
        r["seeded_at"],
        r["contract_version"],
    )


def _pair_keep(g: Generation, current: Contract, previous: Contract | None) -> int:
    # retention by contract: current keeps 2 (cutover overlap), previous keeps 1 (rollback), any
    # other pair keeps its newest so a rolled-back binary always finds a generation
    if g.contract_version == current.version and g.shape == current.fingerprint:
        return _KEEP_CURRENT
    if previous is not None and (g.contract_version, g.shape) == (
        previous.version,
        previous.fingerprint,
    ):
        return _KEEP_PREVIOUS
    return _KEEP_RETIRED


def gc_targets(
    schemas: Iterable[str],
    registered: Iterable[Generation],
    now: datetime,
    *,
    current: Contract = CURRENT,
    previous: Contract | None = PREVIOUS,
    held: frozenset[str] = frozenset(),
    grace: timedelta = _GC_GRACE,
) -> list[str]:
    """gen schema names safe to DROP: past-grace orphans (no registry row -> a dead partial load)
    plus registered generations beyond their contract's retained set (2 current, 1 previous, newest
    1 for any retired pair). within-grace and any schema in `held` (a live lease) are retained too,
    so no pod's pool loses its schema."""
    reg = {g.schema_name: g for g in registered}
    targets: list[str] = []
    for name in schemas:  # orphans: physical gen schema, no registry row, aged past a live load
        if name in reg or name in held:
            continue
        ts = _schema_ts(name)
        if ts is not None and now - ts > grace:
            targets.append(name)
    by_pair: dict[tuple[int, str], list[Generation]] = {}
    for g in reg.values():
        by_pair.setdefault((g.contract_version, g.shape), []).append(g)
    for gens in by_pair.values():
        gens.sort(key=lambda g: g.seeded_at, reverse=True)
        keep = _pair_keep(gens[0], current, previous)  # pair-uniform, so the first row decides
        targets.extend(
            g.schema_name
            for g in gens[keep:]
            if now - g.seeded_at > grace and g.schema_name not in held
        )
    return targets


def should_swap(serving_schema: str, serving_version: int, candidate: Generation | None) -> bool:
    """refresh-tick cutover decision: swap iff `candidate` is a different schema and not a downgrade
    to an older contract than the one currently served (a previous->current upgrade still swaps)."""
    if candidate is None or candidate.schema_name == serving_schema:
        return False
    return candidate.contract_version >= serving_version


async def select_current(
    conn: asyncpg.Connection | asyncpg.Pool,
    *,
    current: Contract = CURRENT,
    previous: Contract | None = PREVIOUS,
) -> Generation | None:
    """newest generation for the current contract pair (contract_version, shape), preferred over any
    newer previous-contract row; else the newest of the previous pair. within a pair newest
    seeded_at wins."""
    if await conn.fetchval("SELECT to_regclass('public.generations')"):
        for contract in _supported(current, previous):
            row = await conn.fetchrow(
                f"SELECT {_SELECT_COLS} FROM public.generations "
                "WHERE contract_version = $1 AND shape = $2 "
                "ORDER BY seeded_at DESC, schema_name DESC LIMIT 1",
                contract.version,
                contract.fingerprint,
            )
            if row is not None:
                return _row_to_gen(row)
    return None


async def register(conn: asyncpg.Connection, gen: Generation) -> datetime:
    # the INSERT is the atomic cutover: new pods/refresh loops see the completed generation at once.
    # seeded_at is DB-clock (DEFAULT now()), never the seeding pod's wall clock -> ordering dodges
    # cross-node skew; returned so the caller times gc against the same authority.
    await conn.execute(GENERATIONS_DDL)
    await conn.execute(LEASES_DDL)
    return await conn.fetchval(
        f"INSERT INTO public.generations ({_INSERT_COLS}) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING seeded_at",
        gen.schema_name,
        gen.shape,
        gen.row_count,
        gen.area_count,
        gen.matrikel_count,
        gen.stednavne_count,
        gen.ejendom_count,
        gen.contract_version,
    )


async def all_generations(conn: asyncpg.Connection) -> list[Generation]:
    if not await conn.fetchval("SELECT to_regclass('public.generations')"):
        return []
    rows = await conn.fetch(f"SELECT {_SELECT_COLS} FROM public.generations")
    return [_row_to_gen(r) for r in rows]


async def existing_schemas(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE $1",
        _SCHEMA_PREFIX.replace("_", r"\_") + "%",
    )
    return [r["nspname"] for r in rows]


async def heartbeat(conn: asyncpg.Connection | asyncpg.Pool, holder: str, schema_name: str) -> None:
    # no DDL here: register() owns the table, so a read-only-ish app role never needs CREATE
    await conn.execute(
        "INSERT INTO public.serving_leases (holder, schema_name) VALUES ($1, $2) "
        "ON CONFLICT (holder) DO UPDATE SET schema_name = EXCLUDED.schema_name, heartbeat = now()",
        holder,
        schema_name,
    )


async def held_schemas(
    conn: asyncpg.Connection, *, window: timedelta = _GC_GRACE
) -> frozenset[str]:
    # schemas a pod heartbeated within `window`; gc must not drop these. empty if no app ran yet.
    if not await conn.fetchval("SELECT to_regclass('public.serving_leases')"):
        return frozenset()
    rows = await conn.fetch(
        "SELECT DISTINCT schema_name FROM public.serving_leases "
        "WHERE heartbeat > now() - $1::interval",  # cast: timestamptz - unknown is ambiguous
        window,
    )
    return frozenset(r["schema_name"] for r in rows)


async def prune_leases(conn: asyncpg.Connection, *, window: timedelta = _GC_GRACE) -> None:
    # drop leases past `window`; oom-killed holders (fresh holder per pod) never self-clear on exit
    if not await conn.fetchval("SELECT to_regclass('public.serving_leases')"):
        return
    await conn.execute(
        "DELETE FROM public.serving_leases WHERE heartbeat < now() - $1::interval", window
    )


async def drop_lease(conn: asyncpg.Connection | asyncpg.Pool, holder: str) -> None:
    if await conn.fetchval("SELECT to_regclass('public.serving_leases')"):
        await conn.execute("DELETE FROM public.serving_leases WHERE holder = $1", holder)
