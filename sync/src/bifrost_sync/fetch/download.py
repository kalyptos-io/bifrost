"""resumable per-entity zip download: .part + http range resume, verify, prune stale generations.

failure taxonomy the worker classifies on: a deterministic 4xx raises SystemExit at once (retrying
repeats it), anything else that exhausts the retries raises RuntimeError so the loop backs off in
minutes instead of sleeping the whole reconcile interval.
"""

from __future__ import annotations

import contextlib
import os
import random
import re
import time
import urllib.error
import urllib.parse
import zipfile

from .catalog import _entity_of, generation
from .session import Session


def _backoff(attempt: int, base: float, cap: float) -> float:
    # exponential + full jitter
    return random.uniform(0, min(cap, base * 2**attempt))


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    # honor integer retry-after on rate-limit / unavailable only
    if e.code not in (429, 503):
        return None
    v = e.headers.get("Retry-After")
    return float(v) if v and v.isdigit() else None


_RETRYABLE_4XX = (408, 416, 429)  # timeout, stale .part range (dropped below), rate limit


def _deterministic(e: urllib.error.HTTPError) -> bool:
    # a 4xx repeats identically every run; retrying it just burns the recovery window
    return 400 <= e.code < 500 and e.code not in _RETRYABLE_4XX


def _zip_ok(path: str) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None
    except (zipfile.BadZipFile, OSError):
        return False


def _verify(path: str, meta: dict) -> bool:
    # byte-size match = complete (tls guards transit); no size -> validate the archive instead
    size = meta.get("fileSizeInBytes")
    if size not in (None, ""):
        try:
            return os.path.getsize(path) == int(size)
        except (TypeError, ValueError, OSError):
            return False
    return _zip_ok(path)


def _stream(session: Session, path: str, part: str) -> None:
    start = os.path.getsize(part) if os.path.exists(part) else 0
    resp = session.open(path, range_start=start or None)
    try:
        status = getattr(resp, "status", None) or resp.getcode()
        append = bool(start) and status == 206  # 200 -> server ignored range, restart from scratch
        with open(part, "ab" if append else "wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
    finally:
        resp.close()


def _prune(work_dir: str, entity: str, keep: str) -> None:
    # drop stale-generation zips (+ their orphaned .part) so a total refresh stays bounded
    for f in os.listdir(work_dir):
        p = os.path.join(work_dir, f)
        if f.startswith(f"{entity}_") and f.endswith((".zip", ".zip.part")) and p != keep:
            with contextlib.suppress(OSError):
                os.remove(p)


def prune_deltas(work_dir: str, entity: str, cursor: int) -> None:
    """drop this entity's delta zips at or below `cursor` - already folded into staging with the
    cursor committed. per-muni total splits ({entity}_{muni}_{gen}.zip) don't match, left alone."""
    pat = re.compile(rf"^{re.escape(entity)}_(\d+)\.zip(?:\.part)?$")
    for f in os.listdir(work_dir):
        m = pat.match(f)
        if m and int(m.group(1)) <= cursor:
            with contextlib.suppress(OSError):
                os.remove(os.path.join(work_dir, f))


def _finalize(work_dir: str, entity: str, part: str, final: str, prune: bool) -> str:
    os.replace(part, final)
    if prune:  # totals keep one zip per entity; a delta run keeps its whole run for reduce
        _prune(work_dir, entity, final)
    return final


def download_entity(
    session: Session,
    meta: dict,
    work_dir: str,
    *,
    retries: int,
    backoff_base: float,
    backoff_cap: float,
    name_override: str | None = None,
    prune: bool = True,
) -> str:
    # name_override gives a per-file basename: mat splits its totals per municipality, so the
    # entity name alone would collide (and _prune would delete the siblings). prune=False keeps a
    # delta run's siblings on disk until reduce has folded them (cli prunes below-cursor after).
    entity, gen = _entity_of(meta), generation(meta)
    base = name_override or entity
    final = os.path.join(work_dir, f"{base}_{gen}.zip")
    if os.path.exists(final) and _verify(final, meta):
        print(f"[i] {base} gen {gen} already present; skipping")
        return final
    part = final + ".part"
    # finalize a complete .part from a crashed prior run (also dodges a Range-at-size 416)
    if os.path.exists(part) and _verify(part, meta):
        print(f"[i] {base} gen {gen} resumed complete; finalizing")
        return _finalize(work_dir, base, part, final, prune)
    path = f"/FileDownloads/v1.0/GetFile?{urllib.parse.urlencode({'Filename': meta['fileName']})}"
    last: object = None
    for attempt in range(retries):
        wait: float | None = None
        try:
            _stream(session, path, part)
            if _verify(part, meta):
                print(f"[+] {base} gen {gen} downloaded")
                return _finalize(work_dir, base, part, final, prune)
            last = "integrity mismatch"
            if os.path.exists(part):
                os.remove(part)  # corrupt/short -> full refetch
        except urllib.error.HTTPError as e:
            if e.code == 416 and os.path.exists(part):
                os.remove(part)  # .part >= server size -> restart from scratch
            if _deterministic(e):
                raise SystemExit(f"[-] {base} download rejected: {e}") from e
            last, wait = e, _retry_after(e)
        except Exception as e:  # transport/stream errors -> retry (partial .part is resumed)
            last = e
        if attempt < retries - 1:
            wait = wait if wait is not None else _backoff(attempt, backoff_base, backoff_cap)
            print(f"[!] {base} attempt {attempt + 1}/{retries} failed ({last}); retry {wait:.0f}s")
            time.sleep(wait)
    # transient by elimination: the worker backs this off in minutes, not a whole interval
    raise RuntimeError(f"[!] {base} download failed after {retries} attempts: {last}")
