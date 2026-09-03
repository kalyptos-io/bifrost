"""bifrost-sync entrypoint.

thin argparse dispatch; all logic lives in fetch/plan/pipeline/snapshot. subcommands:
  sync                 locked one-shot: recover -> plan -> fetch -> stage -> decide -> snapshot
  worker [--interval] [--run-deadline]  long-running: reconcile on an interval with backoff
  status               print the last reconcile's state/phase/error
  baseline [--all|-e]  force a full re-baseline of named entities (ignores cursors)
  snapshot [--force]   derive a generation only (--allow-shrink skips the shrink gate)
  export [--out]       stream the baseline corpus jsonl (corpus record shape) for train.gen
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from time import perf_counter

import asyncpg
from bifrost.db import generations
from bifrost.db.shape import build_fingerprint

from . import export
from . import plan as plan_mod
from . import status as status_mod
from .config import Config
from .fetch import catalog, download
from .fetch.session import Session
from .lock import LockHeld, advisory_lock
from .pipeline import Load, contracts, cursors, drain, make_pipeline, run_loads, staged_rows
from .plan import Action, EntityPlan, needs_snapshot, plan_staging
from .reduce import baseline_rows, reduce_delta_files
from .registers import ALL_ENTITIES, DAGI, DAR, DS, EBR, MAT, EntitySpec
from .snapshot import STAGING
from .snapshot import meta as meta_mod
from .snapshot.build import build_generation
from .snapshot.records import Floors
from .status import Phase, Status
from .worker import Shutdown, run_worker, with_deadline

_DEFAULT_INTERVAL = int(os.environ.get("BIFROST_SYNC_INTERVAL_SECONDS", "86400"))

_FMT = "csv"
_BACKOFF_BASE = 2.0
_BACKOFF_CAP = 60.0


# config + shared options


def _config(args: argparse.Namespace) -> Config:
    cfg = Config.from_env(work_dir=args.work_dir)
    return replace(cfg, dsn=args.dsn) if args.dsn else cfg


def _session(cfg: Config) -> Session:
    if not (cfg.client_id and cfg.client_secret):
        raise SystemExit("[!] DATAFORDELER_CLIENT_ID/DATAFORDELER_CLIENT_SECRET unset")
    return Session(cfg.api_base, cfg.token_url, cfg.client_id, cfg.client_secret)


# fetch


def _downloads_dir(cfg: Config) -> str:
    d = cfg.work_dir / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _download(
    session: Session, meta: dict, work: str, retries: int, name: str | None, *, prune: bool = True
) -> str:
    return download.download_entity(
        session,
        meta,
        work,
        retries=retries,
        backoff_base=_BACKOFF_BASE,
        backoff_cap=_BACKOFF_CAP,
        name_override=name,
        prune=prune,
    )


def _fetch_baseline(
    session: Session, cfg: Config, spec: EntitySpec, listing: list[dict], retries: int
) -> tuple[list[Path], int]:
    """the total's files plus its generation - the cursor is derived from it, never assumed."""
    work = _downloads_dir(cfg)
    if spec.muni_split_totals:  # matriklen splits current totals per municipality
        metas = catalog.mat_muni_totals(listing, spec.entity)
        paths = [
            Path(_download(session, m, work, retries, f"{spec.entity}_{muni}"))
            for muni, m in sorted(metas.items())
        ]
        return paths, min(catalog.generation(m) for m in metas.values())  # oldest muni governs
    meta = catalog.latest_total(listing, spec.entity, _FMT, spec.baseline_variant)
    # a hist spec shares its entity's downloads; download_name keeps its zips (+ prune) distinct
    name = spec.download_name if spec.is_hist else None
    return [Path(_download(session, meta, work, retries, name))], catalog.generation(meta)


def _fetch_deltas(
    session: Session, cfg: Config, spec: EntitySpec, files: list[tuple[int, dict]], retries: int
) -> list[tuple[int, Path]]:
    # prune=False: the whole run ({name}_{gen}.zip, gen distinguishes) must survive for reduce;
    # below-cursor zips are pruned after the load commits (see cmd_sync)
    work = _downloads_dir(cfg)
    name = spec.download_name if spec.is_hist else None
    return [
        (gen, Path(_download(session, m, work, retries, name, prune=False))) for gen, m in files
    ]


# planning + snapshot decision


def _list_catalog(session: Session) -> dict[str, list[dict]]:
    return {s.table: catalog.downloads(session, s.entity, s.register) for s in ALL_ENTITIES}


def _list_deltas(listings: dict[str, list[dict]]) -> list[tuple[EntitySpec, list[dict]]]:
    return [
        (s, catalog.lineage_deltas(listings[s.table], s.entity, _FMT, s.baseline_variant))
        for s in ALL_ENTITIES
    ]


async def _empty_staged(dsn: str) -> set[str]:
    """staging tables currently empty (or absent) - plan_staging distrusts committed cursors over
    them, since a crash/failover can lose a load package's rows after its state committed."""
    conn = await asyncpg.connect(dsn)
    try:
        out: set[str] = set()
        for spec in ALL_ENTITIES:
            table = f'{STAGING}."{spec.table}"'
            if await conn.fetchval("SELECT to_regclass($1)", table) is None or not (
                await conn.fetchval(f"SELECT EXISTS (SELECT 1 FROM {table})")
            ):
                out.add(spec.table)
        return out
    finally:
        await conn.close()


def _log_plan(plans: list[EntityPlan]) -> None:
    by_reg: dict[str, Counter] = defaultdict(Counter)
    for p in plans:
        by_reg[p.spec.register][p.action] += 1
    for reg in (DAR, DAGI, MAT, DS, EBR):
        c = by_reg[reg]
        parts = [f"{c[a]} {a.name.lower()}" for a in Action if c[a]]
        print(f"[i] {reg}: {', '.join(parts) or 'nothing'}")


async def _snapshot(
    cfg: Config,
    cur: dict[str, int],
    con: dict[str, str],
    floors: Floors,
    args: argparse.Namespace,
    *,
    force: bool,
) -> bool:
    fingerprint = build_fingerprint()
    if not force:
        conn = await asyncpg.connect(cfg.dsn)
        try:
            watermark = await meta_mod.read_watermark(conn, staging=STAGING)
            current = await generations.select_current(conn)
        finally:
            await conn.close()
        if not needs_snapshot(cur, con, watermark, current, fingerprint):
            print("[i] watermark current and a matching generation is live; snapshot skipped")
            return False
    try:
        await build_generation(
            cfg,
            cursors=cur,
            contracts=con,
            floors=floors,
            batch_size=args.batch_size,
            allow_shrink=args.allow_shrink,
        )
    except asyncpg.IntegrityConstraintViolationError as exc:
        # inconsistent staging fails identically every build; don't hot-loop it as transient
        raise SystemExit(f"[!] staging integrity violation building generation: {exc}") from exc
    return True


# commands


def _reconcile(cfg: Config, floors: Floors, args: argparse.Namespace, status: Status) -> None:
    pipeline = make_pipeline(cfg)
    print("[i] recovering: draining any pending load package...")  # begin() already set RECOVER
    drain(pipeline)
    session = _session(cfg)
    status.phase(Phase.PLAN)
    print(f"[i] planning {len(ALL_ENTITIES)} entities against committed cursors...")
    listings = _list_catalog(session)
    deltas = _list_deltas(listings)
    # a zero-row load creates no table: absent by design, not a lost load
    empty = asyncio.run(_empty_staged(cfg.dsn)) - {
        t for t, n in staged_rows(pipeline).items() if n == 0
    }
    plans = plan_staging(deltas, cursors(pipeline), contracts(pipeline), empty=empty)
    _log_plan(plans)

    status.phase(Phase.FETCH)
    delta_metas = {spec.table: metas for spec, metas in deltas}  # reused, never re-listed
    baseline_loads: list[Load] = []
    delta_loads: list[Load] = []
    for p in plans:
        if p.action is Action.BASELINE:
            paths, total_gen = _fetch_baseline(
                session, cfg, p.spec, listings[p.spec.table], args.retries
            )
            cursor = plan_mod.cursor_for_total(p.spec.table, delta_metas[p.spec.table], total_gen)
            baseline_loads.append(Load(p.spec, baseline_rows(paths, p.spec), cursor))
        elif p.action is Action.DELTA:
            files = _fetch_deltas(session, cfg, p.spec, p.files, args.retries)
            delta_loads.append(Load(p.spec, reduce_delta_files(files, p.spec), p.new_cursor))

    status.phase(Phase.STAGE)
    if baseline_loads:
        print(f"[i] staging {len(baseline_loads)} baseline resources...")
        run_loads(pipeline, baseline_loads, refresh="drop_resources")
    if delta_loads:
        print(f"[i] staging {len(delta_loads)} delta resources...")
        run_loads(pipeline, delta_loads)
        work = _downloads_dir(cfg)  # cursor committed with the load -> consumed zips are droppable
        for load in delta_loads:
            download.prune_deltas(work, load.spec.download_name, load.cursor)
    staged = len(baseline_loads) + len(delta_loads)
    print(f"[+] staged {staged}, skipped {len(ALL_ENTITIES) - staged}")

    status.phase(Phase.SNAPSHOT)
    cur = cursors(pipeline)
    con = contracts(pipeline)
    if asyncio.run(_snapshot(cfg, cur, con, floors, args, force=False)):
        print("[+] generation registered")


def reconcile_once(cfg: Config, floors: Floors, args: argparse.Namespace) -> None:
    """one full recover->snapshot pass under the advisory lock, with status stamped per phase.
    raises LockHeld if another run holds the lock (before any status write), SystemExit on a
    deterministic failure, or any other Exception on a transient one."""
    status = Status(cfg.dsn)
    with advisory_lock(cfg.dsn):
        status.begin()
        try:
            _reconcile(cfg, floors, args, status)
        except SystemExit as exc:
            status.fail_deterministic(str(exc))
            raise
        except Exception as exc:
            status.fail_transient(str(exc))
            raise
        else:
            status.succeeded()


_LOCK_HELD = "[!] another sync run is active"


def _oneshot(dsn: str, run: Callable[[], None]) -> None:
    # serialize a one-shot command against any concurrent sync/worker run, else fail loudly
    try:
        with advisory_lock(dsn):
            run()
    except LockHeld:
        raise SystemExit(_LOCK_HELD) from None


def cmd_sync(args: argparse.Namespace) -> None:
    # reconcile_once owns the lock (status writes ride inside it), so convert here not via _oneshot
    try:
        reconcile_once(_config(args), Floors(), args)
    except LockHeld:
        raise SystemExit(_LOCK_HELD) from None


def cmd_worker(args: argparse.Namespace) -> None:
    cfg = _config(args)
    floors = Floors()
    shutdown = Shutdown()
    shutdown.install()
    reconcile = with_deadline(lambda: reconcile_once(cfg, floors, args), args.run_deadline)
    run_worker(reconcile, interval=args.interval, shutdown=shutdown, sleep=shutdown.wait)


def cmd_status(args: argparse.Namespace) -> None:
    print(status_mod.render(status_mod.read(_config(args).dsn)))


def _baseline(cfg: Config, args: argparse.Namespace) -> None:
    specs = _selected_specs(args)
    session = _session(cfg)
    pipeline = make_pipeline(cfg)
    drain(pipeline)
    loads: list[Load] = []
    for spec in specs:
        listing = catalog.downloads(session, spec.entity, spec.register)
        metas = catalog.lineage_deltas(listing, spec.entity, _FMT, spec.baseline_variant)
        paths, total_gen = _fetch_baseline(session, cfg, spec, listing, args.retries)
        new_cursor = plan_mod.cursor_for_total(spec.table, metas, total_gen)
        loads.append(Load(spec, baseline_rows(paths, spec), new_cursor))
        print(f"[i] {spec.table}: baseline -> cursor {new_cursor}")
    run_loads(pipeline, loads, refresh="drop_resources")
    print(f"[+] re-baselined {len(loads)} entities into staging")


def cmd_baseline(args: argparse.Namespace) -> None:
    cfg = _config(args)
    _oneshot(cfg.dsn, lambda: _baseline(cfg, args))


def _snapshot_only(cfg: Config, args: argparse.Namespace) -> None:
    pipeline = make_pipeline(cfg)
    pipeline.sync_destination()  # authoritative cursors without re-staging
    cur = cursors(pipeline)
    con = contracts(pipeline)
    if asyncio.run(_snapshot(cfg, cur, con, Floors(), args, force=args.force)):
        print("[+] generation registered")


def cmd_snapshot(args: argparse.Namespace) -> None:
    cfg = _config(args)
    _oneshot(cfg.dsn, lambda: _snapshot_only(cfg, args))


def cmd_export(args: argparse.Namespace) -> None:
    cfg = _config(args)
    out = Path(args.out) if args.out else export.DEFAULT_CORPUS_OUT
    asyncio.run(export.export_jsonl(cfg, out, min_rows=Floors().addresses))


def _selected_specs(args: argparse.Namespace) -> list[EntitySpec]:
    if args.all:
        return list(ALL_ENTITIES)
    if not args.entity:
        raise SystemExit("[!] baseline needs --all or one or more --entity <table|name>")
    by_table = {s.table: s for s in ALL_ENTITIES}
    by_entity = {s.entity.lower(): s for s in ALL_ENTITIES}
    out: list[EntitySpec] = []
    for name in args.entity:
        spec = by_table.get(name) or by_entity.get(name.lower())
        if spec is None:
            raise SystemExit(f"[!] unknown entity '{name}' (want a staging table or entity name)")
        out.append(spec)
    return out


# argparse


def _shared(p: argparse.ArgumentParser) -> None:
    p.add_argument("--work-dir", help="fetch + dlt state dir (env BIFROST_SYNC_WORK_DIR)")
    p.add_argument("--dsn", help="postgres dsn (env BIFROST_DATABASE_DSN)")
    p.add_argument("--retries", type=int, default=int(os.environ.get("BIFROST_SYNC_RETRIES", "5")))
    p.add_argument("--batch-size", type=int, default=50_000, help="address COPY batch size")


def _allow_shrink(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--allow-shrink",
        action="store_true",
        help="promote even if a table shrank against the previous generation",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bifrost-sync", description=__doc__)
    subs = parser.add_subparsers(dest="cmd", required=True)

    sync = subs.add_parser("sync", help="locked one-shot: plan -> fetch -> stage -> snapshot")
    _shared(sync)
    _allow_shrink(sync)
    sync.set_defaults(func=cmd_sync)

    worker = subs.add_parser("worker", help="long-running: reconcile on an interval with backoff")
    _shared(worker)
    worker.set_defaults(allow_shrink=False)  # unattended: never promote a shrunken generation
    worker.add_argument(
        "--interval",
        type=float,
        default=_DEFAULT_INTERVAL,
        help="seconds between successful reconciles (env BIFROST_SYNC_INTERVAL_SECONDS)",
    )
    worker.add_argument(
        "--run-deadline",
        type=int,
        default=status_mod.RUN_DEADLINE,
        help="seconds a reconcile may run before exiting (env BIFROST_SYNC_RUN_DEADLINE_SECONDS)",
    )
    worker.set_defaults(func=cmd_worker)

    stat = subs.add_parser("status", help="print the last reconcile's state/phase/error")
    stat.add_argument("--work-dir", help="fetch + dlt state dir (env BIFROST_SYNC_WORK_DIR)")
    stat.add_argument("--dsn", help="postgres dsn (env BIFROST_DATABASE_DSN)")
    stat.set_defaults(func=cmd_status)

    base = subs.add_parser("baseline", help="force a full re-baseline of named entities")
    _shared(base)
    base.add_argument("--all", action="store_true", help="re-baseline every entity")
    base.add_argument("-e", "--entity", action="append", help="staging table or entity name")
    base.set_defaults(func=cmd_baseline)

    snap = subs.add_parser("snapshot", help="derive a generation only (no fetch/stage)")
    _shared(snap)
    _allow_shrink(snap)
    snap.add_argument("--force", action="store_true", help="build regardless of the watermark")
    snap.set_defaults(func=cmd_snapshot)

    exp = subs.add_parser("export", help="baseline corpus jsonl for train.gen (corpus shape)")
    _shared(exp)
    exp.add_argument("--out", help="corpus jsonl (default train/data/baseline_addresses.jsonl)")
    exp.set_defaults(func=cmd_export)

    return parser


def main() -> None:
    args = _parser().parse_args()
    t0 = perf_counter()
    args.func(args)
    print(f"[+] {args.cmd} done in {perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
