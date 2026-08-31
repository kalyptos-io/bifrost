"""datafordeler fildownload listing: latest totals, mat per-municipality totals, delta runs."""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import NamedTuple

from .session import Session


def downloads(session: Session, entity: str, register: str) -> list[dict]:
    # v2.0 listing (v1.0 retired); rows under availableFileDownloads
    q = urllib.parse.urlencode({"Register": register, "Entity": entity})
    with session.open(f"/FileDownloads/v2.0/GetAvailableFileDownloads?{q}") as resp:
        data = json.load(resp)
    if isinstance(data, list):
        return data
    pages = int((data.get("paginationMetadata") or {}).get("totalPages") or 1)
    if pages > 1:  # server ignores PageSize; truncation would read as a delta gap or stale total
        raise SystemExit(f"[!] {register}/{entity}: paginated listing ({pages} pages) unsupported")
    return data.get("availableFileDownloads") or []


# file-metadata parsing


def generation(meta: dict) -> int:
    g = meta.get("generationNumber")
    if g is not None and str(g).isdigit():
        return int(g)
    m = re.search(r"_(\d+)\.zip$", meta.get("fileName") or "")
    return int(m.group(1)) if m else 0


def _version(meta: dict) -> int:
    v = meta.get("version")
    return int(v) if v is not None and str(v).isdigit() else 0


def _entity_of(meta: dict) -> str:
    n = meta.get("entityName")
    if n:
        return n
    m = re.match(r"[A-Z]+_V\d+_([^_]+)_", meta.get("fileName") or "")
    return m.group(1) if m else "entity"


def _is_national(meta: dict) -> bool:
    # v2 lists per-municipality splits too; national total has no muni code
    return meta.get("municipalityCode") in (None, "")


def _is_type(meta: dict, kind: str) -> bool:
    # kind is "totaldownload" | "deltadownload"; prefer the typeOfDownload field, fall back to name
    t = meta.get("typeOfDownload")
    if t:
        return t.lower() == kind
    return kind in (meta.get("fileName") or "").lower()


def _fmt_match(meta: dict, fmt: str) -> bool:
    return f"_{fmt.lower()}_" in (meta.get("fileName") or "").lower()


# dagi lists header-only stub totals (<=~3.5kb) for unpopulated newer data-model versions; the
# populated product is megabyte-scale. smallest real total (dar postnummer) is ~88kb -> safe floor.
_MIN_TOTAL_BYTES = 10_000


def _has_rows(meta: dict) -> bool:
    s = meta.get("fileSizeInBytes")
    return s in (None, "") or int(s) >= _MIN_TOTAL_BYTES


def latest_total(files: list[dict], entity: str, fmt: str, variant: str) -> dict:
    """latest national TotalDownload for entity in fmt, preferring the requested variant
    (current|bitemporal) and highest data-model version, skipping the empty stub totals the agency
    lists for unpopulated versions. falls back across variant only when no variant-token total
    exists (an entity shipping a single variant may omit the token); the size floor still guards."""

    def ok(meta: dict, want_variant: str | None) -> bool:
        n = (meta.get("fileName") or "").lower()
        return (
            _is_type(meta, "totaldownload")
            and _fmt_match(meta, fmt)
            and _is_national(meta)
            and (want_variant is None or f"_{want_variant.lower()}_" in n)
        )

    cands = [m for m in files if ok(m, variant)] or [m for m in files if ok(m, None)]
    if not cands:
        raise SystemExit(f"[!] no national {fmt} total download for {entity}")
    full = [m for m in cands if _has_rows(m)]
    return max(full or cands, key=lambda m: (_version(m), generation(m)))


def mat_muni_totals(files: list[dict], entity: str, *, fmt_token: str = "_csv_") -> dict[str, dict]:
    """{municipalityCode -> latest current total meta} for one mat entity. matriklen also ships a
    national current csv, but we split per municipality to fold each in bounded memory. prefers the
    highest version, skipping empty-stub totals; falls back to all if none clear the floor."""
    cands: dict[str, list[dict]] = {}
    for m in files:
        n = (m.get("fileName") or "").lower()
        muni = m.get("municipalityCode")
        if not muni or "totaldownload" not in n or fmt_token not in n or "_current_" not in n:
            continue
        cands.setdefault(muni, []).append(m)
    if not cands:
        raise SystemExit(
            f"[!] no per-municipality {fmt_token.strip('_')} current totals for {entity}"
        )
    return {
        muni: max([m for m in ms if _has_rows(m)] or ms, key=lambda m: (_version(m), generation(m)))
        for muni, ms in cands.items()
    }


# delta runs + gap detection


class DeltaPlan(NamedTuple):
    files: list[dict]  # contiguous run to apply, ascending by generation
    gap: bool  # a hole in the run (retention dropped a file) -> caller must re-baseline


def deltas(files: list[dict], fmt: str, version: int) -> list[dict]:
    """national DeltaDownload metas for entity in fmt at one data-model version, ascending by
    generationNumber."""
    by_gen: dict[int, dict] = {}
    for m in files:
        if (
            _is_type(m, "deltadownload")
            and _fmt_match(m, fmt)
            and _is_national(m)
            and _version(m) == version
        ):
            by_gen[generation(m)] = m
    return [by_gen[g] for g in sorted(by_gen)]


def lineage_deltas(files: list[dict], entity: str, fmt: str, variant: str) -> list[dict]:
    """the delta run of the lineage we follow. each data-model version keeps its OWN generation
    counter, so numbers order only within one lineage - mixing them fabricates holes and can walk a
    retired lineage. the total we baseline from is the authority; fall back to the newest lineage
    that actually lists deltas."""
    want = _version(latest_total(files, entity, fmt, variant))
    if run := deltas(files, fmt, want):
        return run
    listed = {_version(m) for m in files if _is_type(m, "deltadownload") and _fmt_match(m, fmt)}
    return deltas(files, fmt, max(listed)) if listed else []


def plan_deltas(metas: list[dict], cursor: int) -> DeltaPlan:
    """the contiguous run of generations > cursor to apply. an empty run (nothing newer) is a clean
    skip; a hole (a generation missing between cursor and the newest) is a gap -> re-baseline."""
    newer = {g: m for m in metas if (g := generation(m)) > cursor}
    if not newer:
        return DeltaPlan([], False)
    gens = sorted(newer)
    if gens != list(range(cursor + 1, gens[-1] + 1)):
        return DeltaPlan([], True)
    return DeltaPlan([newer[g] for g in gens], False)
