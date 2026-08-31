"""sync status: human-readable render (pure) + the sync_meta roundtrip against a real postgres.

the db half lands in a throwaway sync_test_<rand> staging schema (auto-created by the first write,
dropped in teardown), so the real "datafordeler" status row is never touched. dsn-gated.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg2
import pytest
from bifrost.db.contracts import CURRENT
from bifrost_sync.status import Phase, Status, read, render

_DSN = os.environ.get("BIFROST_DATABASE_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="BIFROST_DATABASE_DSN unset")


def test_render_reports_state_phase_and_error():
    out = render(
        {
            "state": "failed_deterministic",
            "phase": "snapshot",
            "started_at": "2026-07-11T00:00:00+00:00",
            "completed_at": "2026-07-11T00:05:00+00:00",
            "desired": {"contract_version": 1, "fingerprint": "abcdef0123456789"},
            "error": "count floors violated",
        }
    )
    assert "[-] state: failed_deterministic" in out
    assert "phase: snapshot" in out
    assert "contract v1 fingerprint abcdef012345" in out
    assert "error: count floors violated" in out


def test_render_handles_missing_record():
    assert render(None) == "[!] no sync status recorded yet"


def _running(minutes_ago: float) -> dict:
    started = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {"state": "running", "phase": "stage", "started_at": started.isoformat()}


def test_render_ages_out_a_running_row_past_the_run_deadline():
    # a SIGKILLed run leaves state=running forever; the advisory lock has long since self-healed
    assert "stale" in render(_running(minutes_ago=500), deadline=6 * 3600)
    assert "stale" not in render(_running(minutes_ago=5), deadline=6 * 3600)
    assert "stale" not in render({"state": "succeeded", "started_at": "2020-01-01T00:00:00+00:00"})
    assert "stale" not in render({"state": "running", "started_at": None})


@pytest.fixture
def staging() -> Iterator[str]:
    name = f"sync_test_{uuid4().hex}"
    yield name
    conn = psycopg2.connect(_DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    conn.close()


@_needs_db
def test_status_lifecycle_persists_each_phase(staging: str):
    st = Status(_DSN, staging=staging)
    st.begin()
    rec = read(_DSN, staging=staging)
    assert rec["state"] == "running" and rec["phase"] == "recover"
    assert rec["desired"]["contract_version"] == CURRENT.version
    assert rec["started_at"] and rec["completed_at"] is None

    st.phase(Phase.STAGE)
    assert read(_DSN, staging=staging)["phase"] == "stage"

    st.succeeded()
    rec = read(_DSN, staging=staging)
    assert rec["state"] == "succeeded" and rec["phase"] == "idle"
    assert rec["completed_at"] is not None


@_needs_db
def test_status_error_is_bounded(staging: str):
    st = Status(_DSN, staging=staging)
    st.begin()
    st.fail_transient("x" * 5000)
    rec = read(_DSN, staging=staging)
    assert rec["state"] == "failed_transient"
    assert len(rec["error"]) == 2000


@_needs_db
def test_read_missing_returns_none(staging: str):
    assert read(_DSN, staging=staging) is None  # no sync_meta table yet
