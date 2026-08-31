"""sync status: a single sync_meta row (key="status") tracking the live/last reconcile run.

reuses the snapshot watermark's key/jsonb table, but writes it synchronously via psycopg2 - each
update opens its own short-lived autocommit connection, so a crash leaves the last phase durably
visible. carries the state machine, the current phase, the contract this build wants to serve, UTC
start/completion stamps, and a bounded latest error. the `status` command renders it human-readably,
ageing out a `running` row past the run deadline (a killed run can't clear its own state).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import StrEnum

import psycopg2
from bifrost.db.contracts import CURRENT
from bifrost.db.shape import build_fingerprint
from psycopg2.extras import Json

from .snapshot import STAGING
from .snapshot.meta import SYNC_META_DDL

_KEY = "status"
_ERROR_MAX = 2000

# the reconcile deadline, shared with the cli's --run-deadline default: a run that outlives it has
# already been hard-exited, so a still-`running` row past it is a leftover, not an owner
RUN_DEADLINE = int(os.environ.get("BIFROST_SYNC_RUN_DEADLINE_SECONDS", "21600"))


class State(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_DETERMINISTIC = "failed_deterministic"


class Phase(StrEnum):
    RECOVER = "recover"
    PLAN = "plan"
    FETCH = "fetch"
    STAGE = "stage"
    SNAPSHOT = "snapshot"
    IDLE = "idle"


def desired_contract() -> dict[str, object]:
    return {"fingerprint": build_fingerprint(), "contract_version": CURRENT.version}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Status:
    """mutable in-memory record flushed whole on every transition; one run owns one Status."""

    def __init__(self, dsn: str, *, staging: str = STAGING):
        self._dsn = dsn
        self._staging = staging
        self._record: dict[str, object] = {}

    def begin(self) -> None:
        self._record = {
            "state": State.RUNNING.value,
            "phase": Phase.RECOVER.value,
            "desired": desired_contract(),
            "started_at": _now(),
            "completed_at": None,
            "error": None,
        }
        self._flush()

    def phase(self, phase: Phase) -> None:
        self._record["phase"] = phase.value
        self._flush()

    def succeeded(self) -> None:
        self._record["state"] = State.SUCCEEDED.value
        self._record["phase"] = Phase.IDLE.value
        self._record["completed_at"] = _now()
        self._flush()

    def fail_transient(self, error: str) -> None:
        self._fail(State.FAILED_TRANSIENT, error)

    def fail_deterministic(self, error: str) -> None:
        self._fail(State.FAILED_DETERMINISTIC, error)

    def _fail(self, state: State, error: str) -> None:
        self._record["state"] = state.value
        self._record["completed_at"] = _now()
        self._record["error"] = error[:_ERROR_MAX]
        self._flush()

    def _flush(self) -> None:
        write(self._dsn, self._record, staging=self._staging)


def write(dsn: str, record: dict[str, object], *, staging: str = STAGING) -> None:
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(SYNC_META_DDL.format(staging=staging))
            cur.execute(
                f'INSERT INTO "{staging}".sync_meta (key, value, updated_at) '
                "VALUES (%s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (_KEY, Json(record)),
            )
    finally:
        conn.close()


def read(dsn: str, *, staging: str = STAGING) -> dict[str, object] | None:
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass(%s)", (f"{staging}.sync_meta",))
            if not cur.fetchone()[0]:
                return None
            cur.execute(f'SELECT value FROM "{staging}".sync_meta WHERE key = %s', (_KEY,))
            row = cur.fetchone()
            return row[0] if row else None  # psycopg2 decodes jsonb to dict
    finally:
        conn.close()


_PREFIX = {
    State.SUCCEEDED.value: "[+]",
    State.RUNNING.value: "[i]",
    State.FAILED_TRANSIENT.value: "[!]",
    State.FAILED_DETERMINISTIC.value: "[-]",
}


def _overdue(started_at: object, deadline: float) -> bool:
    # a SIGKILLed (or deadline-exited) run never clears its row, while the advisory lock self-heals
    try:
        started = datetime.fromisoformat(str(started_at))
        return (datetime.now(UTC) - started).total_seconds() > deadline
    except (TypeError, ValueError):  # unparsable or naive stamp -> not a claim about staleness
        return False


def render(record: dict[str, object] | None, *, deadline: float = RUN_DEADLINE) -> str:
    if not record:
        return "[!] no sync status recorded yet"
    state = str(record.get("state"))
    stale = state == State.RUNNING.value and _overdue(record.get("started_at"), deadline)
    desired = record.get("desired") or {}
    lines = [
        f"{'[!]' if stale else _PREFIX.get(state, '[i]')} state: {state}"
        + (" (stale: no completion within the run deadline; the run is gone)" if stale else ""),
        f"[i] phase: {record.get('phase')}",
        f"[i] started: {record.get('started_at')}",
        f"[i] completed: {record.get('completed_at') or '-'}",
        f"[i] desired: contract v{desired.get('contract_version')} "  # type: ignore[union-attr]
        f"fingerprint {str(desired.get('fingerprint'))[:12]}",  # type: ignore[union-attr]
    ]
    if record.get("error"):
        lines.append(f"[-] error: {record['error']}")
    return "\n".join(lines)
