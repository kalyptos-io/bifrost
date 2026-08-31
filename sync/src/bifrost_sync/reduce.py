"""fold pending delta files (and stream baselines) into staging upserts, per entity.

three fold shapes, dispatched by spec:
- versioned (dar/mat/dagi/ebr main tables): one row per id. per id the latest generation decides;
  among its registration-open rows (blank registreringTil - a superseded correction is excluded) the
  currently-effective version wins (a future-effective version only when none is effective now), so
  a registered future version can't hide a live id behind a preliminary classification. an id with
  zero registration-open rows tombstones. snapshot sql classifies lifecycle later, from status.
- aktualitet (ds stednavn): one row per objectid, latest generation wins; within a generation the
  current (iAnvendelse) version wins over a historisk one. historic skrivemåder (distinct objectids)
  are served as retired rows, so this never tombstones.
- history (*_hist): a version pass-through keyed on (pk, *version_key); a later generation overrides
  the same composite key (a registration-close updates registreringTil in place). history is never
  hard-deleted, so no tombstones.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .extract import SniffState, fold_headers, pk_value, shape_row, to_utc_iso, zip_rows
from .registers import Currency, EntitySpec


def _require_fold_headers(row: dict, spec: EntitySpec, path: Path) -> None:
    missing = [h for h in fold_headers(spec) if h not in row]
    if missing:
        raise SystemExit(f"[!] {path}: delta missing fold header(s): {', '.join(missing)}")


def _version_key_ok(rec: dict, spec: EntitySpec) -> bool:
    # a null composite-key column silently never matches on merge - skip the row (warned at caller)
    keys = (spec.pk_out, *spec.version_key)
    return all(rec.get(k) is not None for k in keys)


@dataclass(slots=True)
class _Best:
    gen: int
    row: dict | None  # shaped best registration-open row; None -> tombstone
    key: tuple[bool, str, str]  # (currently-effective, virkningfra, registreringfra); max wins


def _effective_key(row: dict, now: str) -> tuple[bool, str, str]:
    # currently-effective beats not-effective; within a tier the virkning-latest, then reg-latest
    vfra = to_utc_iso(row.get("virkningFra"))
    vtil = to_utc_iso(row.get("virkningTil"))
    rfra = to_utc_iso(row.get("registreringFra"))
    effective = (vfra is None or vfra <= now) and (vtil is None or vtil > now)
    return (effective, vfra or "", rfra or "")


def _reduce_versioned(files: Sequence[tuple[int, Path]], spec: EntitySpec) -> Iterator[dict]:
    now = datetime.now(UTC).isoformat()
    best: dict[str, _Best] = {}
    for gen, path in files:
        sniff = SniffState(spec)  # headers stable within a file, vary across model versions
        for i, row in enumerate(zip_rows(str(path))):
            if i == 0:  # headers stable within a file, one check suffices
                _require_fold_headers(row, spec, path)
            pk = pk_value(row, spec)
            if pk is None:
                continue
            b = best.get(pk)
            if b is None or gen > b.gen:  # a newer generation decides this id afresh
                b = _Best(gen, None, (False, "", ""))
                best[pk] = b
            elif gen < b.gen:  # already decided by a later generation (defensive; files sorted)
                continue
            if row.get("registreringTil"):  # registration-superseded correction: not a candidate
                continue
            key = _effective_key(row, now)
            if b.row is None or key > b.key:
                b.row, b.key = shape_row(row, spec, sniff), key
    for pk, b in best.items():
        yield b.row if b.row is not None else {spec.pk_out: pk, "_deleted": True}


def _reduce_aktualitet(files: Sequence[tuple[int, Path]], spec: EntitySpec) -> Iterator[dict]:
    latest: dict[str, tuple[int, bool, dict]] = {}
    for gen, path in files:
        sniff = SniffState(spec)
        for i, row in enumerate(zip_rows(str(path))):
            if i == 0:
                _require_fold_headers(row, spec, path)
            pk = pk_value(row, spec)
            if pk is None:
                continue
            cur = row.get("aktualitet") == "iAnvendelse"
            prev = latest.get(pk)
            # newer generation wins outright; within a generation the current version wins
            if prev is None or gen > prev[0] or (gen == prev[0] and cur and not prev[1]):
                latest[pk] = (gen, cur, shape_row(row, spec, sniff))
    for _, _, rec in latest.values():
        yield rec


def _reduce_hist(files: Sequence[tuple[int, Path]], spec: EntitySpec) -> Iterator[dict]:
    out: dict[tuple, tuple[int, dict]] = {}
    for gen, path in files:
        sniff = SniffState(spec)
        warned = False
        for row in zip_rows(str(path)):
            rec = shape_row(row, spec, sniff)
            if not _version_key_ok(rec, spec):
                if not warned:
                    print(f"[!] {path}: {spec.table} rows with null composite key skipped")
                    warned = True
                continue
            key = (rec[spec.pk_out], *(rec[k] for k in spec.version_key))
            cur = out.get(key)
            if cur is None or gen >= cur[0]:  # latest generation overrides the same composite key
                out[key] = (gen, rec)
    for _, rec in out.values():
        yield rec


def reduce_delta_files(files: Sequence[tuple[int, Path]], spec: EntitySpec) -> Iterator[dict]:
    """fold delta files (ascending generation) into staging upserts/tombstones for one entity."""
    if spec.is_hist:
        return _reduce_hist(files, spec)
    if spec.currency is Currency.AKTUALITET:
        return _reduce_aktualitet(files, spec)
    return _reduce_versioned(files, spec)


def baseline_rows(paths: Sequence[Path], spec: EntitySpec) -> Iterator[dict]:
    """stream a full total into upsert rows for a fresh table (no tombstones). classify, never
    filter: every lifecycle state stages, keyed one row per id (a current total) or one row per
    version (a hist spec's bitemporal total); snapshot sql derives lifecycle from the staged status.
    """
    if spec.currency is Currency.AKTUALITET:  # bitemporal total: fold multi-version objectids
        yield from _reduce_aktualitet([(0, p) for p in paths], spec)
        return
    for path in paths:
        sniff = SniffState(spec)
        warned = False
        for row in zip_rows(str(path)):
            rec = shape_row(row, spec, sniff)
            if spec.is_hist and not _version_key_ok(rec, spec):
                if not warned:
                    print(f"[!] {path}: {spec.table} rows with null composite key skipped")
                    warned = True
                continue
            yield rec
