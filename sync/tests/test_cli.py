"""cli catalog reuse across planning and baseline selection, and the cursor a baseline commits."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from types import SimpleNamespace

import pytest
from bifrost_sync import cli
from bifrost_sync.config import Config
from bifrost_sync.registers import ALL_ENTITIES, contract_hash
from bifrost_sync.snapshot.records import Floors


def _spec():
    return next(s for s in ALL_ENTITIES if s.table == "dar_adresse")


def _listing(total: int = 8, deltas: Iterable[int] = (9,)) -> list[dict]:
    return [
        {
            "fileName": f"DAR_V1_Adresse_TotalDownload_csv_Current_{total}.zip",
            "typeOfDownload": "TotalDownload",
            "generationNumber": total,
        },
        *(
            {
                "fileName": f"DAR_V1_Adresse_DeltaDownload_csv_Current_{g}.zip",
                "typeOfDownload": "DeltaDownload",
                "generationNumber": g,
            }
            for g in deltas
        ),
    ]


def _capture_selectors(monkeypatch):
    delta_listings: list[list[dict]] = []
    total_listings: list[list[dict]] = []
    select_deltas = cli.catalog.lineage_deltas
    select_total = cli.catalog.latest_total

    def lineage_deltas(files: list[dict], entity: str, fmt: str, variant: str) -> list[dict]:
        delta_listings.append(files)
        return select_deltas(files, entity, fmt, variant)

    def latest_total(files: list[dict], entity: str, fmt: str, type_: str) -> dict:
        total_listings.append(files)
        return select_total(files, entity, fmt, type_)

    monkeypatch.setattr(cli.catalog, "lineage_deltas", lineage_deltas)
    monkeypatch.setattr(cli.catalog, "latest_total", latest_total)
    return delta_listings, total_listings


def _stub_baseline_io(monkeypatch, tmp_path: Path) -> list:
    staged: list = []
    monkeypatch.setattr(cli, "_download", lambda *_args, **_kwargs: str(tmp_path / "total.zip"))
    monkeypatch.setattr(cli, "baseline_rows", lambda *_args: iter(()))
    monkeypatch.setattr(cli, "run_loads", lambda _p, loads, **_kw: staged.extend(loads))
    return staged


def _stub_reconcile(monkeypatch, tmp_path: Path, spec, listing: list[dict]):
    """stub every I/O edge of _reconcile; returns (session, catalog calls, staged loads)."""
    session = object()
    fetched: list[tuple[object, str, str]] = []

    def downloads(actual_session, entity: str, register: str) -> list[dict]:
        fetched.append((actual_session, entity, register))
        return listing

    async def empty_staged(_: str) -> set[str]:
        return set()

    async def snapshot(*_args, **_kwargs) -> bool:
        return False

    monkeypatch.setattr(cli, "ALL_ENTITIES", (spec,))
    monkeypatch.setattr(cli.catalog, "downloads", downloads)
    monkeypatch.setattr(cli, "make_pipeline", lambda _: object())
    monkeypatch.setattr(cli, "drain", lambda _: None)
    monkeypatch.setattr(cli, "_session", lambda _: session)
    monkeypatch.setattr(cli, "cursors", lambda _: {})
    monkeypatch.setattr(cli, "contracts", lambda _: {})
    monkeypatch.setattr(cli, "_empty_staged", empty_staged)
    monkeypatch.setattr(cli, "staged_rows", lambda _: {})
    monkeypatch.setattr(cli, "_snapshot", snapshot)
    return session, fetched, _stub_baseline_io(monkeypatch, tmp_path)


def _run_reconcile(tmp_path: Path) -> None:
    cli._reconcile(
        Config(dsn="postgresql://test", work_dir=tmp_path),
        Floors(),
        SimpleNamespace(retries=1),
        SimpleNamespace(phase=lambda _: None),
    )


def test_reconcile_shares_listing_between_delta_plan_and_total_selection(monkeypatch, tmp_path):
    spec, listing = _spec(), _listing()
    delta_listings, total_listings = _capture_selectors(monkeypatch)
    session, fetched, _ = _stub_reconcile(monkeypatch, tmp_path, spec, listing)

    _run_reconcile(tmp_path)

    assert fetched == [(session, spec.entity, spec.register)]
    assert delta_listings == [listing]
    # lineage selection reads the total too, so the listing is reused twice - never re-listed
    assert total_listings == [listing, listing]


def test_baseline_cursor_keeps_the_overlap_when_the_total_is_fresh(monkeypatch, tmp_path):
    _, _, staged = _stub_reconcile(
        monkeypatch, tmp_path, _spec(), _listing(total=20, deltas=range(8, 21))
    )
    _run_reconcile(tmp_path)
    assert [load.cursor for load in staged] == [17]  # newest delta - the 3-generation overlap


def test_baseline_cursor_follows_a_lagging_total(monkeypatch, tmp_path):
    # the newest total is 8 generations behind the delta feed: committing the overlap cursor would
    # skip 11..17 forever, so the cursor drops to the total and the next run replays them
    _, _, staged = _stub_reconcile(
        monkeypatch, tmp_path, _spec(), _listing(total=10, deltas=range(8, 21))
    )
    _run_reconcile(tmp_path)
    assert [load.cursor for load in staged] == [10]


def test_baseline_refuses_a_total_below_delta_retention(monkeypatch, tmp_path):
    # deltas 3..7 are gone, so no replay can carry a generation-2 total up to the feed's head
    _stub_reconcile(monkeypatch, tmp_path, _spec(), _listing(total=2, deltas=range(8, 21)))
    with pytest.raises(SystemExit, match="past retention"):
        _run_reconcile(tmp_path)


def test_forced_baseline_shares_listing_between_cursor_and_total_selection(monkeypatch, tmp_path):
    spec = _spec()
    listing = _listing()
    session = object()
    fetched: list[tuple[object, str, str]] = []
    delta_listings, total_listings = _capture_selectors(monkeypatch)

    def downloads(actual_session, entity: str, register: str) -> list[dict]:
        fetched.append((actual_session, entity, register))
        return listing

    monkeypatch.setattr(cli, "ALL_ENTITIES", (spec,))
    monkeypatch.setattr(cli.catalog, "downloads", downloads)
    monkeypatch.setattr(cli, "_session", lambda _: session)
    monkeypatch.setattr(cli, "make_pipeline", lambda _: object())
    monkeypatch.setattr(cli, "drain", lambda _: None)
    staged = _stub_baseline_io(monkeypatch, tmp_path)

    cli._baseline(
        Config(dsn="postgresql://test", work_dir=tmp_path),
        SimpleNamespace(all=False, entity=[spec.table], retries=1),
    )

    assert fetched == [(session, spec.entity, spec.register)]
    assert delta_listings == [listing]
    # lineage selection reads the total too, so the listing is reused twice - never re-listed
    assert total_listings == [listing, listing]
    assert [load.cursor for load in staged] == [6]  # min(9 - 3, total 8)


def test_a_zero_row_load_is_not_read_as_a_lost_load(monkeypatch, tmp_path):
    # upstream has no rows for this entity: the table is legitimately absent, so the committed
    # cursor stands and the tick skips rather than re-baselining it every run
    spec = _spec()
    _, _, staged = _stub_reconcile(monkeypatch, tmp_path, spec, _listing())

    async def empty_staged(_: str) -> set[str]:
        return {spec.table}

    monkeypatch.setattr(cli, "cursors", lambda _: {spec.table: 9})
    monkeypatch.setattr(cli, "contracts", lambda _: {spec.table: contract_hash(spec)})
    monkeypatch.setattr(cli, "_empty_staged", empty_staged)
    monkeypatch.setattr(cli, "staged_rows", lambda _: {spec.table: 0})

    _run_reconcile(tmp_path)

    assert staged == []
