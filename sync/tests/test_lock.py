"""advisory lock contention against a real postgres: a second acquisition fails while the first
holds, and succeeds once released. dsn-gated (skip unset). uses the real lock key on the dev db -
acquired and released within each test, so it never lingers.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from bifrost_sync.cli import reconcile_once
from bifrost_sync.config import Config
from bifrost_sync.lock import LockHeld, advisory_lock
from bifrost_sync.snapshot.records import Floors

_DSN = os.environ.get("BIFROST_DATABASE_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="BIFROST_DATABASE_DSN unset")


@_needs_db
def test_second_acquisition_fails_while_first_holds():
    with advisory_lock(_DSN), pytest.raises(LockHeld), advisory_lock(_DSN):
        pass


@_needs_db
def test_lock_reacquirable_after_release():
    with advisory_lock(_DSN):
        pass
    with advisory_lock(_DSN):  # released on the first with-exit
        pass


@_needs_db
def test_reconcile_once_raises_lock_held_before_any_work(tmp_path):
    # holding the lock, a concurrent reconcile bails at acquisition - never touches staging/status
    cfg = Config(dsn=_DSN, work_dir=tmp_path)
    with advisory_lock(_DSN), pytest.raises(LockHeld):
        reconcile_once(cfg, Floors(), SimpleNamespace())
