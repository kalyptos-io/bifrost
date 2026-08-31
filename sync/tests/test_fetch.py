"""resilient download/resume + retry-after parsing (ported from train/gen/test_registry.py)."""

from __future__ import annotations

import io
import os
import urllib.error

import pytest
from bifrost_sync.fetch import download
from bifrost_sync.fetch.download import _retry_after, download_entity, prune_deltas


class _Resp:
    def __init__(self, blob: bytes, status: int):
        self._buf = io.BytesIO(blob)
        self.status = status

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def getcode(self) -> int:
        return self.status

    def close(self) -> None:
        self._buf.close()


class _FakeSession:
    """serves `blob`, optionally failing the first `fail` opens; honors Range when `ranges`."""

    def __init__(self, blob: bytes, *, fail: int = 0, code: int = 503, ranges: bool = True):
        self.blob, self.fail, self.code, self.ranges = blob, fail, code, ranges
        self.calls = 0
        self.last_range: int | None = None

    def open(self, path: str, *, range_start: int | None = None, timeout: int = 300):
        self.calls += 1
        self.last_range = range_start
        if self.calls <= self.fail:
            raise urllib.error.HTTPError(path, self.code, "x", {}, None)
        if range_start and self.ranges:
            return _Resp(self.blob[range_start:], 206)
        return _Resp(self.blob, 200)


def _meta(blob: bytes, gen: int = 6) -> dict:
    return {
        "fileName": f"DAR_V1_Adresse_TotalDownload_csv_Current_{gen}.zip",
        "entityName": "Adresse",
        "generationNumber": str(gen),
        "fileSizeInBytes": len(blob),
    }


def test_retry_after_honors_int_on_rate_limit():
    assert _retry_after(urllib.error.HTTPError("u", 429, "x", {"Retry-After": "7"}, None)) == 7.0
    assert _retry_after(urllib.error.HTTPError("u", 503, "x", {}, None)) is None
    assert _retry_after(urllib.error.HTTPError("u", 500, "x", {"Retry-After": "7"}, None)) is None


def test_download_writes_and_verifies(tmp_path, monkeypatch):
    monkeypatch.setattr(download.time, "sleep", lambda s: None)
    blob = b"hello-dar" * 1000
    s = _FakeSession(blob)
    out = download_entity(s, _meta(blob), str(tmp_path), retries=4, backoff_base=0, backoff_cap=0)
    assert out.endswith("Adresse_6.zip")
    with open(out, "rb") as f:
        assert f.read() == blob


def test_download_retries_then_succeeds(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(download.time, "sleep", lambda s: slept.append(s))
    blob = b"x" * 500
    s = _FakeSession(blob, fail=2, code=503)
    download_entity(s, _meta(blob), str(tmp_path), retries=5, backoff_base=1, backoff_cap=1)
    assert s.calls == 3 and len(slept) == 2


def test_download_resumes_from_partial(tmp_path):
    blob = b"abcdefghij" * 100
    final = os.path.join(str(tmp_path), "Adresse_6.zip")
    with open(final + ".part", "wb") as f:
        f.write(blob[:400])  # a prior pod left a partial download
    s = _FakeSession(blob, ranges=True)
    download_entity(s, _meta(blob), str(tmp_path), retries=2, backoff_base=0, backoff_cap=0)
    assert s.last_range == 400  # range-appended, not re-fetched from zero
    with open(final, "rb") as f:
        assert f.read() == blob


def test_download_restarts_when_server_ignores_range(tmp_path):
    blob = b"abcdefghij" * 100
    final = os.path.join(str(tmp_path), "Adresse_6.zip")
    with open(final + ".part", "wb") as f:
        f.write(blob[:400])
    s = _FakeSession(blob, ranges=False)  # 200 ignores Range -> truncate + rewrite, not append
    download_entity(s, _meta(blob), str(tmp_path), retries=2, backoff_base=0, backoff_cap=0)
    with open(final, "rb") as f:
        assert f.read() == blob  # full body, not blob[:400]+blob


def test_download_finalizes_complete_part(tmp_path):
    # a prior run streamed the whole body but died before the rename -> finalize, never 416
    blob = b"k" * 250
    final = os.path.join(str(tmp_path), "Adresse_6.zip")
    with open(final + ".part", "wb") as f:
        f.write(blob)
    s = _FakeSession(blob)
    assert (
        download_entity(s, _meta(blob), str(tmp_path), retries=2, backoff_base=0, backoff_cap=0)
        == final
    )
    assert s.calls == 0  # complete .part finalized without any download


def test_download_skips_when_present(tmp_path):
    blob = b"y" * 300
    final = os.path.join(str(tmp_path), "Adresse_6.zip")
    with open(final, "wb") as f:
        f.write(blob)
    s = _FakeSession(blob)
    assert (
        download_entity(s, _meta(blob), str(tmp_path), retries=2, backoff_base=0, backoff_cap=0)
        == final
    )
    assert s.calls == 0  # verified checkpoint -> no download


def test_download_prunes_stale_generation(tmp_path):
    stale = os.path.join(str(tmp_path), "Adresse_5.zip")
    with open(stale, "wb") as f:
        f.write(b"old")
    blob = b"z" * 200
    s = _FakeSession(blob)
    download_entity(s, _meta(blob, gen=6), str(tmp_path), retries=2, backoff_base=0, backoff_cap=0)
    assert not os.path.exists(stale)  # a refresh drops the old generation


def test_download_delta_mode_keeps_siblings(tmp_path):
    # prune=False: a delta run must keep the older gens on disk for reduce to fold
    prior = os.path.join(str(tmp_path), "Adresse_5.zip")
    with open(prior, "wb") as f:
        f.write(b"old")
    blob = b"z" * 200
    s = _FakeSession(blob)
    download_entity(
        s, _meta(blob, gen=6), str(tmp_path), retries=2, backoff_base=0, backoff_cap=0, prune=False
    )
    assert os.path.exists(prior)  # gen 5 survives alongside the new gen 6
    assert os.path.exists(os.path.join(str(tmp_path), "Adresse_6.zip"))


def test_prune_deltas_drops_at_or_below_cursor(tmp_path):
    for name in ("Adresse_4.zip", "Adresse_5.zip", "Adresse_5.zip.part", "Adresse_6.zip"):
        with open(os.path.join(str(tmp_path), name), "wb") as f:
            f.write(b"x")
    # a per-muni total split and another entity must be left alone (don't match the pattern)
    for name in ("Jordstykke_0101_5.zip", "Husnummer_3.zip"):
        with open(os.path.join(str(tmp_path), name), "wb") as f:
            f.write(b"y")
    prune_deltas(str(tmp_path), "Adresse", cursor=5)
    left = sorted(os.listdir(str(tmp_path)))
    assert left == ["Adresse_6.zip", "Husnummer_3.zip", "Jordstykke_0101_5.zip"]


def test_download_integrity_mismatch_exhausts(tmp_path, monkeypatch):
    monkeypatch.setattr(download.time, "sleep", lambda s: None)
    blob = b"q" * 100
    meta = _meta(blob)
    meta["fileSizeInBytes"] = len(blob) + 1  # advertised size never matches -> always short
    s = _FakeSession(blob)
    with pytest.raises(RuntimeError):
        download_entity(s, meta, str(tmp_path), retries=3, backoff_base=0, backoff_cap=0)
    assert not os.path.exists(os.path.join(str(tmp_path), "Adresse_6.zip.part"))  # discarded


def test_download_exhausted_retries_are_transient(tmp_path, monkeypatch):
    # a 5xx outage must not read as deterministic: the worker backs off, it doesn't wait a day
    monkeypatch.setattr(download.time, "sleep", lambda s: None)
    blob = b"w" * 100
    s = _FakeSession(blob, fail=99, code=503)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        download_entity(s, _meta(blob), str(tmp_path), retries=3, backoff_base=0, backoff_cap=0)
    assert s.calls == 3


def test_download_deterministic_4xx_exits_without_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(download.time, "sleep", lambda s: None)
    blob = b"w" * 100
    s = _FakeSession(blob, fail=99, code=404)
    with pytest.raises(SystemExit, match="rejected"):
        download_entity(s, _meta(blob), str(tmp_path), retries=5, backoff_base=0, backoff_cap=0)
    assert s.calls == 1  # no point re-asking for a file the server denies


def test_download_rate_limit_stays_retryable(tmp_path, monkeypatch):
    # 429/408 are 4xx but transient; 416 too (the stale .part is dropped and refetched)
    monkeypatch.setattr(download.time, "sleep", lambda s: None)
    blob = b"w" * 100
    s = _FakeSession(blob, fail=2, code=429)
    download_entity(s, _meta(blob), str(tmp_path), retries=5, backoff_base=0, backoff_cap=0)
    assert s.calls == 3
