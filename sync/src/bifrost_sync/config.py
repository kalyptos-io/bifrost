"""env-backed runtime config: dsn, work dir, datafordeler oauth creds. dev autoloads app/.env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .fetch.session import DEFAULT_BASE, DEFAULT_TOKEN_URL

_APP_ENV = Path(__file__).resolve().parents[3] / "app" / ".env"  # dev-only; real env wins
_WORK_ENV = "BIFROST_SYNC_WORK_DIR"


@dataclass(frozen=True, slots=True)
class Config:
    dsn: str
    work_dir: Path
    client_id: str | None = None  # fetch-only; snapshot/export paths never build a session
    client_secret: str | None = None
    api_base: str = DEFAULT_BASE
    token_url: str = DEFAULT_TOKEN_URL

    @classmethod
    def from_env(cls, *, work_dir: str | Path | None = None) -> Config:
        if _APP_ENV.exists():
            load_dotenv(_APP_ENV)
        dsn = os.environ.get("BIFROST_DATABASE_DSN")
        if not dsn:
            raise SystemExit("[!] BIFROST_DATABASE_DSN unset")
        wd = work_dir or os.environ.get(_WORK_ENV) or Path.cwd() / ".sync-work"
        return cls(
            dsn=dsn,
            work_dir=Path(wd),
            client_id=os.environ.get("DATAFORDELER_CLIENT_ID"),
            client_secret=os.environ.get("DATAFORDELER_CLIENT_SECRET"),
            api_base=os.environ.get("DATAFORDELER_BASE", DEFAULT_BASE),
            token_url=os.environ.get("DATAFORDELER_TOKEN_URL", DEFAULT_TOKEN_URL),
        )
