"""worker loop scheduling + shutdown, exercised with an injected sleep so no real time passes.

the loop is a pure function of (reconcile outcome -> delay); the recorder captures each delay and
ends the loop after a fixed number of cycles by flipping the shutdown flag.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from bifrost_sync import cli
from bifrost_sync.config import Config
from bifrost_sync.lock import LockHeld
from bifrost_sync.worker import Shutdown, run_worker, with_deadline


class _FakeShutdown:
    def __init__(self) -> None:
        self.requested = False


class _Recorder:
    """captures sleep delays; stops the loop after `stop_after` sleeps."""

    def __init__(self, shutdown: _FakeShutdown, stop_after: int):
        self.delays: list[float] = []
        self._shutdown = shutdown
        self._stop_after = stop_after

    def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self._stop_after:
            self._shutdown.requested = True


def _run(reconcile, *, interval: float, stop_after: int) -> list[float]:
    sd = _FakeShutdown()
    rec = _Recorder(sd, stop_after)
    run_worker(reconcile, interval=interval, shutdown=sd, sleep=rec.sleep)
    return rec.delays


def test_reconcile_runs_immediately_on_startup():
    calls: list[int] = []
    delays = _run(lambda: calls.append(1), interval=100, stop_after=1)
    assert calls == [1]  # ran before the first sleep
    assert delays == [100]


def test_success_waits_the_interval():
    assert _run(lambda: None, interval=3600, stop_after=1) == [3600]


def test_deterministic_failure_waits_the_interval():
    def boom() -> None:
        raise SystemExit("count floors violated")

    assert _run(boom, interval=3600, stop_after=1) == [3600]


def test_transient_failure_backs_off_exponentially_to_cap():
    def boom() -> None:
        raise RuntimeError("database is down")

    assert _run(boom, interval=3600, stop_after=8) == [30, 60, 120, 240, 480, 900, 900, 900]


def test_held_lock_is_a_transient_backoff():
    def boom() -> None:
        raise LockHeld()

    assert _run(boom, interval=3600, stop_after=2) == [30, 60]


def test_backoff_resets_after_a_success():
    seq = iter([RuntimeError("x"), RuntimeError("y"), None, RuntimeError("z")])

    def rec() -> None:
        if (exc := next(seq)) is not None:
            raise exc

    assert _run(rec, interval=1000, stop_after=4) == [30, 60, 1000, 30]


def test_shutdown_during_sleep_stops_the_loop():
    calls: list[int] = []
    sd = _FakeShutdown()

    def sleep(_: float) -> None:
        sd.requested = True  # a signal lands mid-sleep

    run_worker(lambda: calls.append(1), interval=100, shutdown=sd, sleep=sleep)
    assert calls == [1]


def test_shutdown_requested_before_first_cycle_runs_nothing():
    calls: list[int] = []
    sd = _FakeShutdown()
    sd.requested = True
    run_worker(lambda: calls.append(1), interval=100, shutdown=sd, sleep=lambda _: None)
    assert calls == []


def test_shutdown_wait_returns_promptly_once_flagged():
    sd = Shutdown()
    sd._handle()  # simulate SIGTERM
    start = time.monotonic()
    sd.wait(10)
    assert time.monotonic() - start < 0.5


def test_shutdown_wait_sleeps_until_the_deadline():
    sd = Shutdown()
    start = time.monotonic()
    sd.wait(0.05, tick=0.01)
    assert time.monotonic() - start >= 0.04


def test_sync_oneshot_fails_loud_when_lock_held(monkeypatch):
    monkeypatch.setattr(cli, "_config", lambda args: Config(dsn="x", work_dir=Path(".")))

    def raise_held(*_a: object) -> None:
        raise LockHeld()

    monkeypatch.setattr(cli, "reconcile_once", raise_held)
    with pytest.raises(SystemExit, match="another sync run is active"):
        cli.cmd_sync(SimpleNamespace())


def test_deadline_aborts_a_wedged_reconcile():
    wedged = threading.Event()
    aborts: list[int] = []

    def abort() -> None:
        aborts.append(1)
        wedged.set()  # release the wedged reconcile the abort just interrupted

    with_deadline(wedged.wait, 0.02, abort=abort)()
    assert aborts == [1]


def test_deadline_cancelled_on_normal_return():
    aborts: list[int] = []
    with_deadline(lambda: None, 60, abort=lambda: aborts.append(1))()
    assert aborts == []


def test_deadline_propagates_failures_and_cancels():
    aborts: list[int] = []

    def boom() -> None:
        raise RuntimeError("reconcile blew up")

    with pytest.raises(RuntimeError):
        with_deadline(boom, 60, abort=lambda: aborts.append(1))()
    assert aborts == []
