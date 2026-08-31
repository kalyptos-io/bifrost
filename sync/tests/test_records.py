"""records ports: to_record coercion parity, dense stable street ids, folded parity, husnr split.
mirrors the retired app/tests/test_db_load.py + train/gen/test_registry.py (no db).
"""

from __future__ import annotations

from bifrost.arms.normalize import normalize
from bifrost.db.generations import Generation
from bifrost_sync.snapshot.records import (
    Counts,
    Floors,
    StreetIds,
    floor_violations,
    ratio_violations,
    shrink_violations,
    split_husnr,
    to_record,
)


def test_split_husnr():
    assert split_husnr("41A") == ("41", "A")
    assert split_husnr("61") == ("61", None)
    assert split_husnr(" 7 ") == ("7", None)
    assert split_husnr("") == (None, None)


def test_to_record_coerces_like_address_from_json():
    rec = to_record(
        {
            "id": "x",
            "street_name": "Vestergade",
            "house_number": 41,
            "postcode": 4850,
            "house_letter": "",
            "floor": None,
            "adgangspunkt_x": "722345.67",  # str -> float
            "adgangspunkt_y": 6179535.68,
            "vejpunkt_x": None,
            "vejpunkt_y": None,
            "kommunekode": "0376",
            "regionskode": 1085,  # int -> str
            "sognekode": "",  # "" -> None
            "jordstykke": 100412345,  # int -> str
            "ejendom_bfe": 100400001,  # int -> str
        },
        StreetIds(),
    )
    assert rec is not None
    assert rec[3] == "41"  # int house_number -> str
    assert rec[2] == "4850"  # int postcode -> str
    assert rec[4] is None  # house_letter "" -> None
    assert rec[5] is None  # floor None -> None
    assert rec[8] == 722345.67  # adgangspunkt_x str -> float
    assert rec[9] == 6179535.68  # adgangspunkt_y float kept
    assert rec[10] is None  # vejpunkt_x None -> None
    assert rec[12] == "0376"  # kommunekode carried
    assert rec[13] == "1085"  # int regionskode -> str
    assert rec[14] is None  # sognekode "" -> None
    assert rec[15] is None  # district codes absent -> None (stamped in a later slice)
    assert rec[18] == "100412345"  # jordstykke int -> str
    assert rec[19] == "100400001"  # ejendom_bfe int -> str
    assert rec[20] is None  # city absent -> None
    assert rec[21] == "current"  # lifecycle defaults to current
    assert len(rec) == 22  # ...+ city + lifecycle


def test_to_record_skips_rows_without_street_or_husnr():
    assert to_record({"id": "x", "house_number": 5}, StreetIds()) is None  # NOT NULL street missing
    assert to_record({"id": "x", "street_name": "Vej"}, StreetIds()) is None  # NOT NULL husnr


def test_street_ids_deterministic_dense_stable_with_dim():
    ids = StreetIds()
    assert ids.id_for("Vestergade", "vestergade") == 0
    assert ids.id_for("Nørregade", "noerregade") == 1
    assert ids.id_for("Vestergade", "vestergade") == 0  # same folded name -> same id
    assert len(ids) == 2  # dense, no gaps
    assert ids.dim_records() == [
        (0, "Vestergade", "vestergade", "current"),
        (1, "Nørregade", "noerregade", "current"),
    ]


def test_street_ids_keep_best_lifecycle_across_addresses():
    # a name seen via a retired then a current address collapses to a current street_dim entry
    ids = StreetIds()
    ids.id_for("Møllevej", "moellevej", "retired")
    ids.id_for("Møllevej", "moellevej", "current")
    assert ids.dim_records()[0] == (0, "Møllevej", "moellevej", "current")


def test_street_ids_lookup_never_mints():
    ids = StreetIds()
    ids.id_for("Hovedgaden", normalize("Hovedgaden"))
    assert ids.lookup(normalize("Hovedgaden")) == 0
    assert ids.lookup(normalize("Ukendtvej")) is None  # orphan geom, no address -> not minted
    assert len(ids) == 1


def test_folded_street_parity_lands_in_the_dim():
    # folded_street MUST equal serving's normalize() of the street, or trigram match degrades
    ids = StreetIds()
    to_record({"id": "x", "street_name": "Teglværksvej", "house_number": 1}, ids)
    _sid, street, folded, _life = ids.dim_records()[0]
    assert street == "Teglværksvej"  # raw display kept
    assert folded == normalize("Teglværksvej") == "teglvaerksvej"


def test_to_record_field_mapping():
    rec = to_record(
        {
            "id": "uuid-1",
            "street_name": "Vestergade",
            "house_number": 41,
            "sub_locality": "Brønshøj",
            "postcode": 2700,
        },
        StreetIds(),
    )
    assert rec is not None
    assert rec[0] == "uuid-1"  # id -> address_id
    assert rec[1] == 0  # street_id
    assert rec[7] == "Brønshøj"  # sub_locality (raw display, renamed bynavn)


def test_floor_violations_flags_short_tables_only():
    floors = Floors()
    ok = Counts(
        addresses=3_900_000,
        areas=4_000,
        matrikel=2_500_000,
        stednavne=150_000,
        ejendom=2_400_000,
        sfe=2_100_000,
        ejerlejlighed=320_000,
        bpfg=31_000,
        ebr_stamped=210_000,
        aux_postcode_dim=1_100,
    )
    assert floor_violations(ok, floors) == []
    short = Counts(
        addresses=10,
        areas=0,
        matrikel=0,
        stednavne=0,
        ejendom=0,
        sfe=0,
        ejerlejlighed=0,
        bpfg=0,
        ebr_stamped=0,
        aux_postcode_dim=0,
    )
    v = floor_violations(short, floors)
    assert len(v) == 10 and any(x.startswith("addresses") for x in v)


def _gen(name: str, addresses: int, *, ejendom: int = 2_700_000) -> Generation:
    return Generation(name, "shape", addresses, 4_000, 2_600_000, 144_000, ejendom, None)


def test_shrink_violations_catch_a_drop_the_absolute_floor_misses():
    floors = Floors()
    prior = _gen("gen_old", 3_940_000)
    short = _gen("gen_new", 3_600_000)  # lost 8.6% of the register
    assert short.row_count > floors.addresses  # ...and still clears the absolute floor
    v = shrink_violations(short, prior, floors)
    assert len(v) == 1 and v[0].startswith("addresses") and "gen_old" in v[0]
    assert shrink_violations(_gen("gen_new", 3_900_000), prior, floors) == []  # 1% drift is fine
    assert shrink_violations(_gen("gen_new", 4_100_000), prior, floors) == []  # growth is fine


def test_shrink_violations_skip_without_a_prior_generation():
    assert shrink_violations(_gen("gen_first", 1), None, Floors()) == []


def test_shrink_violations_report_every_shrunken_table():
    v = shrink_violations(_gen("gen_new", 1, ejendom=1), _gen("gen_old", 3_940_000), Floors())
    assert len(v) == 2  # addresses + ejendom; the untouched counts don't fire


def test_ratio_violations_gate_only_above_the_limit():
    counts = {"skipped": (6, 1000), "clean": (0, 1000), "empty": (0, 0)}
    assert ratio_violations(counts, 0.005) == ["skipped 6/1000 = 0.6% > 0.5%"]
    assert ratio_violations(counts, 0.01) == []  # 0.6% is inside a 1% budget
    assert ratio_violations({"regionskode": (1000, 1000)}, 0.10)  # an all-null column always fires
