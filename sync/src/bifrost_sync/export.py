"""corpus jsonl export, re-derived from live staging via iter_addresses.

that join needs matrikel + the PIP _district_stamp present in a schema, so a throwaway
sync_export_<rand> schema is built to carry just those two joins, then dropped. reading staging
directly (not a registered generation) is what reproduces the full 19-field record: a
registered gen has dropped _district_stamp and never stores city (it's derived at serve time).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import TextIO
from uuid import uuid4

import asyncpg
from bifrost.db import schema_sql

from .config import Config
from .snapshot import STAGING
from .snapshot.addresses import ensure_hist_indexes, iter_addresses
from .snapshot.build import READER_TUNE, WRITER_TUNE
from .snapshot.districts import stamp_districts
from .snapshot.lifecycle import CURRENT
from .snapshot.matrikel import load_matrikel

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CORPUS_OUT = _REPO_ROOT / "train" / "data" / "baseline_addresses.jsonl"


@asynccontextmanager
async def _join_scaffold(
    cfg: Config, *, staging: str = STAGING
) -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    """a throwaway schema applying full schema_sql (ejendom stays empty) + _district_stamp so
    iter_addresses can join; yields (reader, schema), drops schema + closes connections on exit."""
    schema = f"sync_export_{uuid4().hex}"  # disjoint from gen_* (gc-invisible) and datafordeler*
    reader = await asyncpg.connect(cfg.dsn)
    writer = await asyncpg.connect(cfg.dsn)
    try:
        await reader.execute(READER_TUNE)
        await writer.execute(WRITER_TUNE)
        await writer.execute(f'CREATE SCHEMA "{schema}"')
        await writer.execute(f'SET search_path TO "{schema}", public')
        await writer.execute(schema_sql())
        await load_matrikel(reader, writer, schema, staging=staging)  # currency gate for jordstykke
        await stamp_districts(reader, writer, schema, staging=staging)
        await ensure_hist_indexes(writer, staging=staging)  # writer: the reader pins on stream
        yield reader, schema
    finally:
        with contextlib.suppress(Exception):
            await writer.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await reader.close()
        await writer.close()


@contextmanager
def _writer(path: Path) -> Iterator[TextIO]:  # open in sync scope (blocking i/o off the async body)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        yield f


async def export_jsonl(
    cfg: Config, out_path: str | Path, *, min_rows: int = 0, staging: str = STAGING
) -> int:
    """stream the corpus per-address records to out_path; SystemExit if short of min_rows."""
    out = Path(out_path)
    total = 0
    async with _join_scaffold(cfg, staging=staging) as (reader, schema):
        with _writer(out) as f:
            async for rec in iter_addresses(reader, schema, staging=staging):
                if (rec.get("lifecycle") or CURRENT) != CURRENT:  # the corpus is current-only
                    continue
                rec = {k: v for k, v in rec.items() if k != "lifecycle"}  # not in the corpus shape
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                total += 1
                if total % 500_000 == 0:
                    print(f"[i] {total} written...")
    if total < min_rows:
        raise SystemExit(f"[!] short export: {total} < {min_rows}")
    print(f"[+] wrote {total} addresses to {out}")
    return total
