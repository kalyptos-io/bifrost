"""long-running sync worker: reconcile on a fixed interval, backing off on transient failures.

wraps a one-shot reconcile callable (which owns the advisory lock + status writes) in a loop that
classifies failures - a SystemExit is deterministic (floor/validation; wait the normal interval),
any other exception is transient (db/net/os or a held lock; exponential backoff 30s doubling to a
15min cap). the reconcile releases every db connection + the lock before returning, so the worker
sleeps holding nothing. sigterm/sigint flip a flag checked between cycles and interrupt the sleep,
so shutdown is clean when idle between phases. a signal mid-reconcile is honored only at the next
cycle boundary, so under k8s' default 30s grace a mid-load pod is SIGKILLed - data-safe: dlt resumes
the staging load and the unregistered gen_<ts> is gc'd. a per-run deadline also bounds a wedged
reconcile (hard exit; the pod restarts), since the loop itself has no timeout on the reconcile call.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable

_BACKOFF_START = 30.0
_BACKOFF_CAP = 900.0  # 15 min


class Shutdown:
    """sigterm/sigint -> requested; wait() is an interruptible chunked sleep on the same flag."""

    def __init__(self) -> None:
        self._flag = False

    def install(self) -> None:
        signal.signal(signal.SIGTERM, self._handle)
        signal.signal(signal.SIGINT, self._handle)

    def _handle(self, *_: object) -> None:
        print("[i] shutdown requested; finishing the current phase...")
        self._flag = True

    @property
    def requested(self) -> bool:
        return self._flag

    def wait(self, seconds: float, *, tick: float = 1.0) -> None:
        # poll the flag every tick so a signal ends a long interval promptly, platform-neutrally
        deadline = time.monotonic() + seconds
        while not self._flag and (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(tick, remaining))


def run_worker(
    reconcile: Callable[[], None],
    *,
    interval: float,
    shutdown: Shutdown,
    sleep: Callable[[float], None],
) -> None:
    backoff = _BACKOFF_START
    print(f"[i] sync worker starting; reconcile interval {interval:.0f}s")
    while not shutdown.requested:
        try:
            reconcile()
        except SystemExit as exc:
            print(f"[-] deterministic failure; waiting the interval: {exc}")
            delay, backoff = interval, _BACKOFF_START
        except Exception as exc:  # transient: db/net/os or a held lock
            delay, backoff = backoff, min(backoff * 2, _BACKOFF_CAP)
            print(f"[!] transient failure; backing off {delay:.0f}s: {exc}")
        else:
            print(f"[+] reconcile complete; next run in {interval:.0f}s")
            delay, backoff = interval, _BACKOFF_START
        if shutdown.requested:
            break
        sleep(delay)
    print("[i] sync worker stopped")


def with_deadline(
    fn: Callable[[], None], seconds: float, *, abort: Callable[[], None] | None = None
) -> Callable[[], None]:
    """wrap fn with a daemon-timer deadline; abort fires on overrun, cancelled on return/raise."""

    def _default_abort() -> None:
        # hard exit: timer thread can't raise into blocked c calls / dlt / asyncio snapshot island
        print(f"[-] reconcile exceeded deadline ({seconds:.0f}s), exiting")
        os._exit(1)

    def wrapped() -> None:
        timer = threading.Timer(seconds, abort or _default_abort)
        timer.daemon = True
        timer.start()
        try:
            fn()
        finally:
            timer.cancel()  # exceptions must propagate for run_worker's backoff classifier

    return wrapped
