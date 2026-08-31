"""self-check for the playground span->style mapping."""

from __future__ import annotations

from .playground import STYLE, apply_spans, compact


def test_apply_spans_styles_ranges() -> None:
    raw = "Valby Langgade 92"
    spans = [("street", 0, 14), ("house_number", 15, 17)]
    t = apply_spans(raw, spans)
    assert t.plain == raw
    got = {(s.start, s.end, s.style) for s in t.spans}
    assert got == {(0, 14, STYLE["street"]), (15, 17, STYLE["house_number"])}


def test_compact_tags_spans() -> None:
    raw = "Valby Langgade 92, 3 th"
    spans = [("street", 0, 14), ("house_number", 15, 17), ("floor", 19, 20), ("door", 21, 23)]
    assert compact(raw, spans).plain == "[st]Valby Langgade[num]92[flr]3[dr]th"
