"""compose structured fields into a surface string with exact component spans.

surface is an ordered list of (text, label) segments; label=None marks structural
delimiters. char spans are derived after all mutation (see mutate.py), so length-changing
corruptions never desync offsets and we never substring-search full_address.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, replace

from .address import Address


@dataclass(slots=True)
class Segment:
    text: str
    label: str | None = None  # None = structural delimiter


def render(segs: list[Segment]) -> str:
    return "".join(s.text for s in segs)


def spans(segs: list[Segment]) -> list[dict]:
    out: list[dict] = []
    off = 0
    for s in segs:
        n = len(s.text)
        if s.label is not None and n:
            out.append({"start": off, "end": off + n, "label": s.label})
        off += n
    return out


def _floor_token(floor: str, rng: random.Random) -> str:
    p = 0.6 if floor.isdigit() else 0.25  # st / kl take a trailing dot less often than digits
    return floor + "." if rng.random() < p else floor


# users often omit optional unit information even when the registry carries it
P_LETTER = 0.42
P_UNIT = 0.42  # floor/door shown together
P_SUBLOC = 0.5


# delimiter inserted *before* a component, keyed by its label
def _delim(right: str | None, rng: random.Random) -> str:
    if right == "house_letter":
        return "" if rng.random() < 0.88 else " "
    if right == "postcode":
        r = rng.random()
        return ", " if r < 0.84 else (" " if r < 0.91 else ",")
    if right == "floor":
        return ", " if rng.random() < 0.12 else " "
    if right == "sub_locality":
        return ", "
    return " "


def compose(a: Address, rng: random.Random) -> list[Segment]:
    comps: list[Segment] = []
    if a.street_name:
        comps.append(Segment(a.street_name, "street"))
    if a.house_number:
        comps.append(Segment(a.house_number, "house_number"))
        if a.house_letter and rng.random() < P_LETTER:
            comps.append(Segment(a.house_letter, "house_letter"))
    show_unit = rng.random() < P_UNIT
    if a.floor and show_unit:
        comps.append(Segment(_floor_token(a.floor, rng), "floor"))
    if a.door and show_unit:
        comps.append(Segment(a.door, "door"))
    if a.sub_locality and rng.random() < P_SUBLOC:
        comps.append(Segment(a.sub_locality, "sub_locality"))
    if a.postcode:
        comps.append(Segment(a.postcode, "postcode"))
    if a.city:
        comps.append(Segment(a.city, "city"))

    segs: list[Segment] = [comps[0]]  # anchor on first present comp; partials may drop street
    for cur in comps[1:]:
        d = _delim(cur.label, rng)
        if d:
            segs.append(Segment(d, None))
        segs.append(cur)
    return segs


# each shape lists fields to KEEP; rest dropped (street -> "", others -> None)
_PARTIAL_SHAPES: dict[str, tuple[str, ...]] = {
    "P1": ("postcode", "city"),  # 9800 Hjørring
    "P2": ("street_name", "house_number"),  # Algade 5
    "P3": ("street_name", "house_number", "city"),  # Algade 5, Hjørring
    "P4": ("postcode",),  # 9800
    "P5": ("city",),  # Hjørring
}
_FIELDS = (
    "street_name",
    "house_number",
    "house_letter",
    "floor",
    "door",
    "sub_locality",
    "postcode",
    "city",
)

P_PARTIAL = 0.044
PARTIAL_WEIGHTS = {"P1": 0.4, "P2": 0.25, "P3": 0.15, "P4": 0.1, "P5": 0.1}

# autocomplete "field-skip": street then jump to a later field, dropping interior (husnr/postcode).
# prefix_cut cannot express this shape, so it has its own rate.
_FIELD_SKIP_SHAPES: dict[str, tuple[str, ...]] = {
    "F1": ("street_name", "city"),  # strandgade koebenhavn
    "F2": ("street_name", "postcode", "city"),  # strandgade 2100 koebenhavn
    "F3": ("street_name", "postcode"),  # strandgade 2100
}
FIELD_SKIP_WEIGHTS = {"F1": 0.5, "F2": 0.3, "F3": 0.2}
P_FIELD_SKIP = 0.06


def _weighted[T](items: list[tuple[T, float]], rng: random.Random) -> T:
    r = rng.random() * sum(w for _, w in items)
    acc = 0.0
    for key, w in items:
        acc += w
        if r < acc:
            return key
    return items[-1][0]


def partial_drop(
    a: Address,
    rng: random.Random,
    weights: dict[str, float] = PARTIAL_WEIGHTS,
    shapes: dict[str, tuple[str, ...]] = _PARTIAL_SHAPES,
) -> Address | None:
    """reduce to a valid kept-field shape; None if no shape's kept fields all present."""
    viable = [
        (s, weights.get(s, 0.0))
        for s, keep in shapes.items()
        if weights.get(s, 0.0) > 0 and all(getattr(a, f) for f in keep)
    ]
    if not viable:
        return None
    keep = set(shapes[_weighted(viable, rng)])
    return replace(a, **{f: ("" if f == "street_name" else None) for f in _FIELDS if f not in keep})


# P(truncate a finished surface to a typed prefix); a capability, not a log-calibrated rate
P_TYPED = 0.22


def prefix_cut(segs: list[Segment], rng: random.Random) -> list[Segment]:
    """truncate to a typed prefix: cut at a back-weighted char offset, slice the straddling seg."""
    raw = render(segs)
    if not raw:
        return segs
    n = len(raw)
    k = n - int(n * rng.random() ** 2)  # back-weighted: autocomplete states cluster near-complete
    out: list[Segment] = []
    off = 0
    for s in segs:
        end = off + len(s.text)
        if end <= k:
            out.append(s)
            off = end
            continue
        text = s.text[: k - off]
        if text:  # boundary-aligned k leaves an empty slice; drop it, no zero-len litter
            out.append(Segment(text, s.label))
        break
    return out
