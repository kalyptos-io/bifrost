"""listing/selection: latest_total (ported), delta runs + gap detection, mat per-muni totals."""

from __future__ import annotations

import io
import json

import pytest
from bifrost_sync.fetch import catalog
from bifrost_sync.fetch.catalog import deltas, latest_total, mat_muni_totals, plan_deltas


class _FakeSession:
    """returns a fixed listing payload from open(); enough to drive downloads directly."""

    def __init__(self, payload: object):
        self._body = json.dumps(payload)

    def open(self, path: str):
        return io.StringIO(self._body)  # a StringIO is its own context manager


# downloads response-shape handling: bare list, paginated abort, single page


def test_downloads_tolerates_bare_list():
    session = _FakeSession([{"fileName": "x"}])
    assert catalog.downloads(session, "Adresse", "DAR") == [{"fileName": "x"}]


def test_downloads_aborts_on_multipage_listing():
    # server ignores PageSize; a >1-page listing would silently truncate -> fail loud
    payload = {"availableFileDownloads": [], "paginationMetadata": {"totalPages": 2}}
    with pytest.raises(SystemExit):
        catalog.downloads(_FakeSession(payload), "Adresse", "DAR")


def test_downloads_single_page_returns_rows():
    payload = {
        "availableFileDownloads": [{"fileName": "x"}],
        "paginationMetadata": {"totalPages": 1},
    }
    assert catalog.downloads(_FakeSession(payload), "Adresse", "DAR") == [{"fileName": "x"}]


# file-metadata parsing (live-confirmed shape: typeOfDownload field, int generationNumber)


def test_is_type_prefers_field_over_filename():
    assert catalog._is_type({"typeOfDownload": "DeltaDownload"}, "deltadownload")
    assert not catalog._is_type({"typeOfDownload": "TotalDownload"}, "deltadownload")
    # no field -> fall back to the filename token
    assert catalog._is_type({"fileName": "X_DeltaDownload_csv_9.zip"}, "deltadownload")


def test_generation_accepts_int_and_falls_back_to_filename():
    assert catalog.generation({"generationNumber": 690}) == 690  # api returns an int
    assert catalog.generation({"generationNumber": "8"}) == 8
    fn = "DS_V4_Stednavn_DeltaDownload_csv_Bitemporal_679.zip"
    assert catalog.generation({"fileName": fn}) == 679


def test_is_national_treats_null_muni_as_national():
    assert catalog._is_national({"municipalityCode": None})
    assert catalog._is_national({"municipalityCode": ""})
    assert not catalog._is_national({"municipalityCode": "0751"})


# latest-total selection (ported)


def test_latest_total_selects_the_requested_variant():
    files = [
        {"fileName": "DAR_V1_Adresse_TotalDownload_csv_Current_5.zip", "generationNumber": "5"},
        {"fileName": "DAR_V1_Adresse_TotalDownload_csv_Current_8.zip", "generationNumber": "8"},
        {"fileName": "DAR_V1_Adresse_DeltaDownload_csv_Current_9.zip", "generationNumber": "9"},
        {"fileName": "DAR_V1_Adresse_TotalDownload_csv_Bitemporal_7.zip", "generationNumber": "7"},
        {"fileName": "DAR_V1_Adresse_TotalDownload_json_Current_9.zip", "generationNumber": "9"},
    ]
    # newest csv current total, ignoring delta + the json + the bitemporal one
    assert latest_total(files, "Adresse", "csv", "current")["generationNumber"] == "8"
    # both variants present -> the requested one wins, no fallback
    assert latest_total(files, "Adresse", "csv", "bitemporal")["generationNumber"] == "7"


def test_latest_total_falls_back_across_variant_when_token_absent():
    # an entity shipping a single variant may omit the token; fall back to the sole national total
    # rather than crash the baseline. the size floor still applies (populated over stub)
    files = [
        {
            "fileName": "DAR_V1_Adresse_TotalDownload_csv_5.zip",
            "generationNumber": "5",
            "fileSizeInBytes": "88000",
        },
        {
            "fileName": "DAR_V1_Adresse_TotalDownload_csv_6.zip",
            "generationNumber": "6",
            "fileSizeInBytes": "408",  # stub -> skipped by the floor
        },
    ]
    assert latest_total(files, "Adresse", "csv", "bitemporal")["generationNumber"] == "5"


def test_latest_total_skips_empty_stub_totals():
    # agency lists header-only stubs for newer (unpopulated) versions; the data lives in v1
    files = [
        {
            "fileName": "DAGI_V4_Kommuneinddeling_TotalDownload_csv_Current_123.zip",
            "version": "4",
            "generationNumber": "123",
            "fileSizeInBytes": "408",
        },
        {
            "fileName": "DAGI_V3_Kommuneinddeling_TotalDownload_csv_Current_123.zip",
            "version": "3",
            "generationNumber": "123",
            "fileSizeInBytes": "408",
        },
        {
            "fileName": "DAGI_V1_Kommuneinddeling_TotalDownload_csv_Current_673.zip",
            "version": "1",
            "generationNumber": "673",
            "fileSizeInBytes": "13252518",
        },
    ]
    picked = latest_total(files, "Kommuneinddeling", "csv", "current")
    assert picked["generationNumber"] == "673"  # the populated one, not the higher-version stub


def test_latest_total_raises_when_format_absent():
    with pytest.raises(SystemExit):
        latest_total(
            [{"fileName": "DAR_V1_Adresse_TotalDownload_json_Current_8.zip"}],
            "Adresse",
            "csv",
            "current",
        )


# delta listing


def _delta(gen: int, *, version: str = "4", muni: str | None = None, fmt: str = "csv") -> dict:
    return {
        "fileName": f"DAR_V{version}_Adresse_DeltaDownload_{fmt}_Bitemporal_{gen}.zip",
        "typeOfDownload": "DeltaDownload",
        "generationNumber": gen,
        "version": version,
        "municipalityCode": muni,
    }


def test_deltas_filters_and_orders():
    files = [
        _delta(680),
        _delta(678),
        {  # a total is not a delta
            "fileName": "DAR_V4_Adresse_TotalDownload_csv_Temporal_684.zip",
            "typeOfDownload": "TotalDownload",
            "generationNumber": 684,
        },
        _delta(679, fmt="json"),  # wrong format
        _delta(679, muni="0751"),  # per-municipality split, not national
        _delta(679, version="3"),  # another lineage
        _delta(679),
    ]
    got = deltas(files, "csv", 4)
    assert [catalog.generation(m) for m in got] == [678, 679, 680]  # ascending, national csv only
    assert {m["version"] for m in got} == {"4"}


def test_deltas_keeps_one_lineage_off_the_shared_number_line():
    # live dagi shape: the retired lineage carries the HIGHER generations on its own counter
    files = [_delta(g, version="4") for g in (175, 176)] + [
        _delta(g, version="1") for g in (742, 743)
    ]
    assert [catalog.generation(m) for m in deltas(files, "csv", 4)] == [175, 176]
    assert [catalog.generation(m) for m in deltas(files, "csv", 1)] == [742, 743]


# lineage selection


def _total(gen: int, version: str) -> dict:
    return {
        "fileName": f"DAR_V{version}_Adresse_TotalDownload_csv_Current_{gen}.zip",
        "typeOfDownload": "TotalDownload",
        "generationNumber": gen,
        "version": version,
        "fileSizeInBytes": 2_000_000,
    }


def test_lineage_follows_the_total_not_the_highest_generation():
    files = [
        _total(189, "4"),
        *(_delta(g, version="4") for g in (188, 189)),
        *(_delta(g, version="1") for g in (742, 743)),  # retired lineage, higher numbers
    ]
    got = catalog.lineage_deltas(files, "Adresse", "csv", "current")
    assert [catalog.generation(m) for m in got] == [188, 189]


def test_lineage_falls_back_when_the_total_lineage_lists_no_deltas():
    files = [_total(189, "4"), *(_delta(g, version="3") for g in (188, 189))]
    got = catalog.lineage_deltas(files, "Adresse", "csv", "current")
    assert {m["version"] for m in got} == {"3"}


# delta run planning + gap detection


def test_plan_deltas_returns_contiguous_run_above_cursor():
    metas = [_delta(g) for g in (688, 689, 690)]
    plan = plan_deltas(metas, cursor=687)
    assert plan.gap is False
    assert [catalog.generation(m) for m in plan.files] == [688, 689, 690]


def test_plan_deltas_skips_generations_at_or_below_cursor():
    metas = [_delta(g) for g in (685, 686, 687, 688)]
    plan = plan_deltas(metas, cursor=686)
    assert plan.gap is False
    assert [catalog.generation(m) for m in plan.files] == [687, 688]


def test_plan_deltas_empty_when_nothing_newer():
    metas = [_delta(g) for g in (688, 689, 690)]
    plan = plan_deltas(metas, cursor=690)
    assert plan == catalog.DeltaPlan([], False)  # up to date -> clean skip


def test_plan_deltas_gap_when_first_delta_beyond_cursor():
    # retention dropped 688: the run starts at 689, not cursor+1 -> gap -> caller must re-baseline
    metas = [_delta(g) for g in (689, 690)]
    plan = plan_deltas(metas, cursor=687)
    assert plan.gap is True and plan.files == []


def test_plan_deltas_gap_on_hole_inside_run():
    # 689 missing between 688 and 690 -> non-contiguous -> gap
    metas = [_delta(g) for g in (688, 690)]
    plan = plan_deltas(metas, cursor=687)
    assert plan.gap is True and plan.files == []


# mat per-municipality current totals


def _mat_total(muni: str, gen: int, *, version: str = "3", size: str = "1212020") -> dict:
    return {
        "fileName": f"MAT_V{version}_Jordstykke_{muni}_TotalDownload_csv_Current_{gen}.zip",
        "typeOfDownload": "TotalDownload",
        "generationNumber": str(gen),
        "version": version,
        "municipalityCode": muni,
        "fileSizeInBytes": size,
    }


def test_mat_muni_totals_picks_latest_per_muni():
    files = [
        _mat_total("0101", 662),
        _mat_total("0101", 663),  # newer gen for same muni -> wins
        _mat_total("0751", 663),
        _mat_total("0751", 663, version="4", size="408"),  # higher version but empty stub -> skip
        {  # national history-only total is not per-muni
            "fileName": "MAT_V3_Jordstykke_TotalDownload_csv_Current_663.zip",
            "typeOfDownload": "TotalDownload",
            "generationNumber": "663",
            "municipalityCode": None,
        },
        {  # a gpkg split is the wrong format
            "fileName": "MAT_V3_Jordstykke_0101_TotalDownload_gpkg_Current_663.zip",
            "municipalityCode": "0101",
        },
    ]
    got = mat_muni_totals(files, "Jordstykke")
    assert set(got) == {"0101", "0751"}
    assert got["0101"]["generationNumber"] == "663"
    assert got["0751"]["version"] == "3"  # the populated v3, not the empty v4 stub


def test_mat_muni_totals_raises_without_per_muni_split():
    with pytest.raises(SystemExit):
        mat_muni_totals([_mat_total("", 663)], "Jordstykke")  # only national -> no per-muni split
