"""tiered, family-based mutations over the segment list.

each family is (segs, rng) -> (segs, applied). spans are recomputed from segments after
mutation, so families freely change text length. orchestrator fires families by per-tier rate.

tier 1 = the observed noise distribution (the floor).
tier 2 = the same families pushed past observed rates / stacked.
tier 3 = noise of another nature, rare or synthetic-only.
"""

from __future__ import annotations

import functools
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

from bifrost.arms.normalize import (
    _MARKER,
    _PUNCT,
    _repair_mojibake,
    _url_decode,
    fold,
)

from .compose import Segment, render
from .junk import junk_segment

Family = Callable[[list[Segment], random.Random], "tuple[list[Segment], bool]"]


@dataclass(frozen=True)
class NoiseCfg:
    """knobs over the family catalogue; DEFAULT is the standard corpus mix."""

    intensity: float = 1.0  # scale per-family table prob; p>=1.0 families (casing) only scale down
    p_partial: float = 0.0  # P(emit a structural partial); 0 = full only
    p_field_skip: float = 0.0  # P(field-skip: street + a later field, interior dropped)
    p_typed: float = 0.0  # P(truncate to a typed prefix); 0 = full only
    p_no_target: float = 0.0


DEFAULT = NoiseCfg()


def _map_text(segs: list[Segment], fn: Callable[[str], str]) -> list[Segment]:
    return [Segment(fn(s.text), s.label) for s in segs]


def _delim_idx(segs: list[Segment]) -> list[int]:
    return [i for i, s in enumerate(segs) if s.label is None]


# ---- final canonicalization: training surface == normalize(query) ----
# segmenter only sees normalized text, so the training surface must pass the SAME normalize, here
# re-expressed over the segment list (spans stay a pure function of segments); pinned by test_gen.


def _strip_recipient_segs(segs: list[Segment]) -> list[Segment]:
    # mirror normalize._strip_recipient on segments: split comma-blocks (commas live in None-delims
    # so never cut a span), drop a block iff it carries a marker and no digit. parity with serving.
    blocks: list[list[Segment]] = []
    cur: list[Segment] = []
    for s in segs:
        if s.label is None and "," in s.text:
            parts = s.text.split(",")
            cur.append(Segment(parts[0], None))
            blocks.append(cur)
            blocks.extend([Segment(p, None)] for p in parts[1:-1])
            cur = [Segment(parts[-1], None)]
        else:
            cur.append(s)
    blocks.append(cur)
    if len(blocks) < 2:
        return segs  # no comma -> single block (kept, or marker-only -> never strip to empty)
    kept = [
        b for b in blocks if any(c.isdigit() for c in render(b)) or not _MARKER.search(render(b))
    ]
    if not kept:
        return segs  # never strip to empty
    out: list[Segment] = []
    for bi, b in enumerate(kept):
        if bi:
            out.append(Segment(" ", None))  # rejoin spacing is loose; final collapse fixes it
        out.extend(b)
    return out


def _collapse_ws_segs(segs: list[Segment]) -> list[Segment]:
    # collapse whitespace + trim; the inter-token space gets its own delimiter, never a labeled span
    out: list[Segment] = []
    started = pending = False
    for s in segs:
        toks = s.text.split()
        if not toks:
            pending = pending or (started and bool(s.text))
            continue
        if started and (pending or s.text[:1].isspace()):
            out.append(Segment(" ", None))
        out.append(Segment(" ".join(toks), s.label))
        started, pending = True, s.text[-1:].isspace()
    return out


def _strip_pad_segs(segs: list[Segment]) -> list[Segment]:
    removed: set[int] = set()
    for match in re.finditer(r"\b0+(\d)", render(segs)):
        removed.update(range(match.start(), match.end() - 1))
    out, offset = [], 0
    for segment in segs:
        text = "".join(
            char for index, char in enumerate(segment.text, offset) if index not in removed
        )
        out.append(Segment(text, segment.label))
        offset += len(segment.text)
    return out


def normalize_segs(segs: list[Segment]) -> list[Segment]:
    segs = _map_text(segs, _url_decode)
    surface = render(segs)
    if _repair_mojibake(surface) != surface:  # all-or-nothing: per-seg iff whole repairs
        segs = _map_text(segs, _repair_mojibake)
    segs = _map_text(segs, str.lower)
    segs = _strip_recipient_segs(segs)
    segs = _map_text(segs, lambda t: t.translate(_PUNCT))
    segs = _map_text(segs, fold)
    segs = _collapse_ws_segs(segs)
    segs = _strip_pad_segs(segs)
    return [s for s in segs if s.text]  # drop emptied segments (e.g. punct-only junk -> "")


# ---- tier 1: floor families ----


def casing(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    def jitter(t: str) -> str:
        return "".join(c.lower() if rng.random() < 0.3 else c for c in t)

    r = rng.random()
    if r < 0.006:
        f: Callable[[str], str] = str.upper
    elif r < 0.010:
        f = str.lower
    elif r < 0.14:
        f = jitter
    else:
        return segs, False
    return _map_text(segs, f), True


def ascii_fold(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    return _map_text(segs, fold), True  # shared canonical fold (parity with the serving normalizer)


def _lower_hex(s: str) -> str:  # %C3%B8 -> %c3%b8
    return re.sub(r"%[0-9A-Fa-f]{2}", lambda m: m.group(0).lower(), s)


# tagger collapses on unseen percent-encodings; tier 1 stays standard, tiers 2/3 sweep the set
_URL_STD: tuple[Callable[[str], str], ...] = (lambda t: quote(t, safe=""),)
_URL_VARIANTS: tuple[Callable[[str], str], ...] = (
    lambda t: quote(t, safe=""),  # standard: upper hex, %20
    lambda t: _lower_hex(quote(t, safe="")),  # lowercase hex
    lambda t: quote_plus(t, safe=""),  # + for space (form-encoding)
    lambda t: t.replace(" ", "%20"),  # partial: spaces only, letters raw
    lambda t: quote(quote(t, safe=""), safe=""),  # double-encoded (%2520)
)


def url_encode(
    segs: list[Segment], rng: random.Random, variants: tuple[Callable[[str], str], ...] = _URL_STD
) -> tuple[list[Segment], bool]:
    return _map_text(segs, rng.choice(variants)), True


def double_space(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    cand = [i for i in _delim_idx(segs) if " " in segs[i].text]
    if not cand:
        return segs, False
    i = rng.choice(cand)
    segs = list(segs)
    segs[i] = Segment(segs[i].text.replace(" ", "  ", 1), None)
    return segs, True


def space_before_comma(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    cand = [i for i in _delim_idx(segs) if "," in segs[i].text and not segs[i].text.startswith(" ")]
    if not cand:
        return segs, False
    i = rng.choice(cand)
    segs = list(segs)
    segs[i] = Segment(" " + segs[i].text, None)
    return segs, True


def drop_space_after_comma(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    cand = [i for i in _delim_idx(segs) if segs[i].text == ", "]
    if not cand:
        return segs, False
    i = rng.choice(cand)
    segs = list(segs)
    segs[i] = Segment(",", None)
    return segs, True


def drop_comma(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    cand = [i for i in _delim_idx(segs) if "," in segs[i].text]
    if not cand:
        return segs, False
    i = rng.choice(cand)
    segs = list(segs)
    segs[i] = Segment(" ", None)
    return segs, True


def trailing(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    return [*segs, Segment(" " if rng.random() < 0.7 else ", ", None)], True


def edge_punctuation(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    token = rng.choice(("*", "#", "/", "-", ":"))
    if rng.random() < 0.5:
        return [Segment(token + " ", None), *segs], True
    return [*segs, Segment(" " + token, None)], True


def country_suffix(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    return [*segs, Segment(", ", None), Segment(rng.choice(("DK", "Danmark")), "junk")], True


def unit_notation(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    cand = [i for i, s in enumerate(segs) if s.label in ("floor", "door") and s.text]
    if not cand:
        return segs, False
    i = rng.choice(cand)
    before, current, after = segs[:i], segs[i], segs[i + 1 :]
    text = current.text.rstrip(".")
    kind = rng.randrange(3)
    if kind == 0:
        replacement = [Segment(f"{text}.", current.label)]
    elif kind == 1:
        replacement = [Segment(text, current.label), Segment(",", None)]
    else:
        replacement = [Segment(f"{text}.", current.label), Segment(" ", None)]
    return [*before, *replacement, *after], True


_ABBR = {
    "Gammel": "Gl.",
    "Søndre": "Sdr.",
    "Nordre": "Ndr.",
    "Vestre": "V.",
    "Østre": "Ø.",
    "Sankt": "Sct.",
    "Kongens": "Kgs.",
    "Store": "St.",
    "Lille": "Ll.",
    "Gammelt": "Gl.",
}


def abbreviate(
    segs: list[Segment], rng: random.Random, abbr: dict[str, str] = _ABBR
) -> tuple[list[Segment], bool]:
    out: list[Segment] = []
    applied = False
    for s in segs:
        t = s.text
        if s.label == "street" and not applied:
            for k, v in abbr.items():
                if t.startswith(k + " "):
                    t = v + " " + t[len(k) + 1 :]
                    applied = True
                    break
        out.append(Segment(t, s.label))
    return out, applied


def truncate(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    labeled = [i for i, s in enumerate(segs) if s.label]
    if len(labeled) <= 1:
        return segs, False
    cut = rng.randint(1, len(labeled) - 1)
    return segs[: labeled[cut] + 1], True


def junk_prefix(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    return [junk_segment(rng), Segment(", ", None), *segs], True


def junk_suffix(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    sep = ", " if rng.random() < 0.5 else " "  # never glued: "5Hansen" is an unrealistic surface
    return [*segs, Segment(sep, None), junk_segment(rng)], True


# ---- tier 2: same-kind escalation ----


def reorder(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    # permute comma-blocks while preserving their internal segments (and prior noise)
    blocks: list[list[Segment]] = [[]]
    seps: list[Segment] = []
    for s in segs:
        if s.label is None and "," in s.text:
            seps.append(s)
            blocks.append([])
        else:
            blocks[-1].append(s)
    if len(blocks) < 2:
        return segs, False
    order = list(range(len(blocks)))
    rng.shuffle(order)
    if order == list(range(len(blocks))):
        return segs, False
    out: list[Segment] = []
    for bi, idx in enumerate(order):
        if bi:
            out.append(seps[bi - 1])
        out.extend(blocks[idx])
    return out, True


def swap_postcode_city(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    pc = next((i for i, s in enumerate(segs) if s.label == "postcode"), None)
    ci = next((i for i, s in enumerate(segs) if s.label == "city"), None)
    if pc is None or ci is None:
        return segs, False
    segs = list(segs)
    segs[pc], segs[ci] = segs[ci], segs[pc]
    return segs, True


def token_split(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    # split a component value with a stray space: "Vestergade" -> "Vester gade"
    # postcode excluded: it's a fixed 4-digit token, a "98 00" span teaches a non-4-digit postcode
    cand = [i for i, s in enumerate(segs) if s.label and s.label != "postcode" and len(s.text) >= 4]
    if not cand:
        return segs, False
    i = rng.choice(cand)
    t = segs[i].text
    p = rng.randrange(2, len(t) - 1)
    return [*segs[:i], Segment(t[:p] + " " + t[p:], segs[i].label), *segs[i + 1 :]], True


def token_merge(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    # glue components by dropping a whitespace delimiter: "9800 Hjørring" -> "9800Hjørring"
    cand = [i for i in _delim_idx(segs) if segs[i].text.isspace()]
    if not cand:
        return segs, False
    i = rng.choice(cand)
    segs = list(segs)
    segs[i] = Segment("", None)
    return segs, True


def multi_junk(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    segs, _ = junk_prefix(segs, rng)
    if rng.random() < 0.5:
        segs, _ = junk_prefix(segs, rng)
    return segs, True


def duplicate(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    # repeat a postcode/city component: "2000 2000 Frederiksberg"
    cand = [i for i, s in enumerate(segs) if s.label in ("postcode", "city")]
    if not cand:
        return segs, False
    i = rng.choice(cand)
    copy = Segment(segs[i].text, segs[i].label)
    return [*segs[: i + 1], Segment(" ", None), copy, *segs[i + 1 :]], True


# ---- tier 3: different-kind (novel) ----

_KB = {}
for _row in ["qwertyuiopå", "asdfghjklæø", "zxcvbnm"]:
    for _j, _c in enumerate(_row):
        nb = ""
        if _j:
            nb += _row[_j - 1]
        if _j + 1 < len(_row):
            nb += _row[_j + 1]
        _KB[_c] = nb


def _edit_one(
    segs: list[Segment], rng: random.Random, fn: Callable[[str], str]
) -> tuple[list[Segment], bool]:
    cand = [i for i, s in enumerate(segs) if s.label and s.text.strip()]
    if not cand:
        return segs, False
    i = rng.choice(cand)
    t = segs[i].text
    p = rng.randrange(len(t))
    new = fn(t[p])
    segs = list(segs)
    segs[i] = Segment(t[:p] + new + t[p + 1 :], segs[i].label)
    return segs, True


def typo(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    return _edit_one(segs, rng, lambda c: rng.choice(_KB[c.lower()]) if _KB.get(c.lower()) else c)


def transpose(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    cand = [i for i, s in enumerate(segs) if s.label and len(s.text) >= 2]
    if not cand:
        return segs, False
    i = rng.choice(cand)
    t = list(segs[i].text)
    p = rng.randrange(len(t) - 1)
    t[p], t[p + 1] = t[p + 1], t[p]
    return [*segs[:i], Segment("".join(t), segs[i].label), *segs[i + 1 :]], True


_OCR = {"l": "1", "o": "0", "O": "0", "I": "1", "S": "5", "B": "8", "g": "9"}


def ocr(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    return _edit_one(segs, rng, lambda c: _OCR.get(c, c))


_CHARS = "abcdefghijklmnopqrstuvwxyzæøå0123456789"


def chaos(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    segs = list(segs)
    for _ in range(rng.randint(1, 4)):
        # postcode excluded: char-chaos on a 4-digit token is unreal + fold could break its length
        cand = [i for i, s in enumerate(segs) if s.label and s.text and s.label != "postcode"]
        if not cand:
            break
        i = rng.choice(cand)
        t = list(segs[i].text)
        op = rng.choice(["ins", "del", "sub", "swap"])
        p = rng.randrange(len(t))
        if op == "del" and len(t) > 1:
            t.pop(p)
        elif op == "ins":
            t.insert(p, rng.choice(_CHARS))
        elif op == "swap" and p + 1 < len(t):
            t[p], t[p + 1] = t[p + 1], t[p]
        else:
            t[p] = rng.choice(_CHARS)
        segs[i] = Segment("".join(t), segs[i].label)
    return segs, True


_MOJI = {"ø": "Ã¸", "æ": "Ã¦", "å": "Ã¥", "Ø": "Ã\x98", "Æ": "Ã\x86", "Å": "Ã\x85", "é": "Ã©"}


def mojibake(segs: list[Segment], rng: random.Random) -> tuple[list[Segment], bool]:
    return _map_text(segs, lambda t: "".join(_MOJI.get(c, c) for c in t)), True


# (family, fire-probability) per tier. casing/fold gate on content, so their configured rates
# compensate for addresses they cannot affect.
_TIER: dict[int, list[tuple[Family, float]]] = {
    1: [
        (casing, 1.0),
        (ascii_fold, 0.03),
        (abbreviate, 0.30),
        (url_encode, 0.047),
        (double_space, 0.071),
        (space_before_comma, 0.07),
        (drop_space_after_comma, 0.043),
        (drop_comma, 0.08),
        (trailing, 0.03),
        (edge_punctuation, 0.01),
        (country_suffix, 0.004),
        (unit_notation, 0.02),
        (truncate, 0.05),
        (swap_postcode_city, 0.006),
        (junk_prefix, 0.05),
        (junk_suffix, 0.02),
        (token_merge, 0.01),
        (duplicate, 0.005),
    ],
    2: [
        (casing, 1.0),
        (ascii_fold, 0.5),
        (token_split, 0.4),
        (token_merge, 0.3),
        (abbreviate, 0.5),
        (drop_comma, 0.4),
        (double_space, 0.3),
        (reorder, 0.25),
        (swap_postcode_city, 0.1),
        (truncate, 0.2),
        (multi_junk, 0.4),
        (junk_suffix, 0.12),
        (duplicate, 0.03),
        (edge_punctuation, 0.08),
        (country_suffix, 0.02),
        (unit_notation, 0.08),
        (functools.partial(url_encode, variants=_URL_VARIANTS), 0.1),
    ],
    3: [
        (casing, 1.0),
        (typo, 0.7),
        (transpose, 0.5),
        (ocr, 0.5),
        (chaos, 0.6),
        (mojibake, 0.3),
        (token_split, 0.3),
        (token_merge, 0.25),
        (drop_comma, 0.4),
        (reorder, 0.2),
        (swap_postcode_city, 0.1),
        (truncate, 0.3),
        (multi_junk, 0.3),
        (junk_suffix, 0.12),
        (edge_punctuation, 0.12),
        (country_suffix, 0.04),
        (unit_notation, 0.12),
        (functools.partial(url_encode, variants=_URL_VARIANTS), 0.15),
    ],
}


def _name(fn: object) -> str:
    # unwraps functools.partial
    return getattr(fn, "__name__", None) or fn.func.__name__  # ty: ignore[unresolved-attribute]


def tier_with(family: str) -> int:
    for t in (3, 2, 1):  # highest tier = most complete config (e.g. url_encode variant set)
        if any(_name(fn) == family for fn, _ in _TIER[t]):
            return t
    raise SystemExit(f"no tier contains family {family!r}")


def mutate(
    segs: list[Segment], tier: int, rng: random.Random, cfg: NoiseCfg = DEFAULT
) -> tuple[list[Segment], list[str]]:
    base = render(segs)
    applied: list[str] = []
    for fn, p in _TIER[tier]:
        if rng.random() < p * cfg.intensity:
            before = render(segs)
            new, ok = fn(segs, rng)
            if ok and render(new) != before:  # only record families that visibly changed raw
                segs = new
                applied.append(_name(fn))
    if render(segs) == base:  # edits cancelled out -> honestly clean
        applied = []
    return segs, applied
