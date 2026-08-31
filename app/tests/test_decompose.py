"""decompose adapter: segmenter spans -> Decomposition. segment() stubbed (no artifact needed)."""

import bifrost.arms.decompose as decompose_mod
from bifrost.arms.decompose import decompose


def _stub_spans(spans):
    return lambda _text: spans


def test_maps_spans_to_fields(monkeypatch):
    text = "vestergade 41a, 4850 stubbekoebing"
    spans = [
        ("street", 0, 10),
        ("house_number", 11, 13),
        ("house_letter", 13, 14),
        ("postcode", 16, 20),
        ("city", 21, 34),
    ]
    monkeypatch.setattr(decompose_mod, "segment", _stub_spans(spans))
    d = decompose(text)
    assert d.text == text
    assert d.street == "vestergade"
    assert d.house_number == "41"
    assert d.house_letter == "a"
    assert d.postcode == "4850"
    assert d.city == "stubbekoebing"
    assert d.sub_locality is None


def test_sub_locality_span_maps_and_junk_dropped(monkeypatch):
    text = "c/o hans jensen, algade 5 hasle"
    spans = [
        ("junk", 0, 15),
        ("street", 17, 23),
        ("house_number", 24, 25),
        ("sub_locality", 26, 31),
    ]
    monkeypatch.setattr(decompose_mod, "segment", _stub_spans(spans))
    d = decompose(text)
    assert d.street == "algade"
    assert d.house_number == "5"
    assert d.sub_locality == "hasle"
    assert d.city is None  # junk contributed nothing


def test_multiple_same_label_spans_join_in_order(monkeypatch):
    text = "skt hans gade 2"
    spans = [("street", 0, 3), ("street", 4, 13)]  # split street, rejoined in document order
    monkeypatch.setattr(decompose_mod, "segment", _stub_spans(spans))
    assert decompose(text).street == "skt hans gade"


def test_empty_segmentation_yields_bare_decomposition(monkeypatch):
    monkeypatch.setattr(decompose_mod, "segment", _stub_spans([]))
    d = decompose("???")
    assert d.text == "???"
    assert d.street is None and d.postcode is None
