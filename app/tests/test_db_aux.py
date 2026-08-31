"""aux tests: floor/door synonym vocab + the AuxMaps row-grouping (registry maps load per gen)."""

from bifrost.arms.aux_index import AuxMaps
from bifrost.arms.normalize import normalize
from bifrost.db.aux import DOOR_SYNONYMS, FLOOR_SYNONYMS


def test_aux_maps_group_rows_into_frozensets() -> None:
    aux = AuxMaps.from_rows(
        postcode_dim=["6900", "1050", "9999"],
        city_rows=[("koebenhavn k", "1050"), ("skjern", "6900"), ("koebenhavn k", "1051")],
        subloc_rows=[("husum", "2700")],
    )
    assert aux.city_map["koebenhavn k"] == frozenset({"1050", "1051"})  # pairs grouped by name
    assert aux.subloc_map == {"husum": frozenset({"2700"})}
    assert aux.postcode_dim == ["1050", "6900", "9999"]  # sorted, city-less 9999 retained


def test_floor_synonyms_canonicalize() -> None:
    assert FLOOR_SYNONYMS["stuen"] == "st"  # ground floor
    assert FLOOR_SYNONYMS["kaelder"] == "kld"  # folded kælder -> basement
    assert FLOOR_SYNONYMS["st"] == "st"  # canonical maps to itself


def test_door_synonyms_canonicalize() -> None:
    assert DOOR_SYNONYMS["venstre"] == "tv"
    assert DOOR_SYNONYMS["hoejre"] == "th"  # folded højre
    assert DOOR_SYNONYMS["midtfor"] == "mf"


def test_synonym_keys_are_folded() -> None:
    # keys must be normalizer output so serving-time lookups match (no raw æøå / mixed case)
    for k in (*FLOOR_SYNONYMS, *DOOR_SYNONYMS):
        assert normalize(k) == k
