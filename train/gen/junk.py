"""recipient/company junk: never resolved, labeled 'junk' so the segmenter learns to isolate it."""

from __future__ import annotations

import random

from .compose import Segment
from .names_da import (
    COMPANY_FORMS,
    COMPANY_WORDS,
    GIVEN,
    GIVEN_TAIL,
    PATRONYM_STEMS,
    SURNAMES,
    TOPO,
    TOPO_SUF,
)

_PREFIX = ["c/o ", "att: ", "att. ", "v/ ", "", "", ""]

# junk-variant mix (rest -> recipient names/companies): noise basin, numeric strings not to read as
# postcode, delivery free-text not to read as an address.
P_NOISE = 0.18
P_NUMERIC = 0.12
P_INSTRUCTION = 0.10
_MASH = "abcdefghijklmnopqrstuvwxyzæøå0123456789"
_SYMBOLS = "!?.;:*#-/\\"  # no comma/space: keep noise one contiguous junk token

# delivery notes / unit qualifiers - free text that is never an address component
_INSTRUCTION = (
    "ring på",
    "ring på døren",
    "bagindgang",
    "for enden af vejen",
    "gul dør",
    "rød dør",
    "til venstre",
    "til højre",
    "ved siden af",
    "efterlad hos nabo",
    "læg i postkassen",
    "kontakt før levering",
    "ingen reklamer",
    "stuen til venstre",
    "opgang",
    "lejlighed",
    "værelse",
    "indgang",
)


def _noise(rng: random.Random) -> str:
    kind = rng.randint(0, 3)
    if kind == 0:  # symbol run
        return rng.choice(_SYMBOLS) * rng.randint(2, 6)
    if kind == 1:  # keyboard mash
        return "".join(rng.choice(_MASH) for _ in range(rng.randint(4, 14)))
    if kind == 2:  # alnum soup with stray symbols
        return "".join(rng.choice(_MASH + _SYMBOLS) for _ in range(rng.randint(4, 14)))
    return rng.choice(_SYMBOLS).join(  # short tokens glued by punctuation
        "".join(rng.choice(_MASH) for _ in range(rng.randint(1, 4)))
        for _ in range(rng.randint(2, 3))
    )


def _digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def _numeric(rng: random.Random) -> str:
    # phone / cvr / reference numbers: long digit runs the segmenter must reject, not call postcode
    kind = rng.randint(0, 3)
    if kind == 0:
        d = _digits(rng, 8)
        return rng.choice([d, f"{d[:2]} {d[2:4]} {d[4:6]} {d[6:]}", f"tlf {d}"])  # phone
    if kind == 1:
        return f"cvr {_digits(rng, 8)}"
    if kind == 2:
        return f"{rng.choice(('ref', 'ordre', 'id', 'nr'))} {_digits(rng, rng.randint(5, 10))}"
    return _digits(rng, rng.randint(5, 10))  # bare long run (never 4 -> never a postcode)


def _instruction(rng: random.Random) -> str:
    t = rng.choice(_INSTRUCTION)
    return f"{t} {rng.randint(1, 40)}" if rng.random() < 0.35 else t  # "lejlighed 4", "opgang 2"


def _surname(rng: random.Random) -> str:
    r = rng.random()
    if r < 0.55:
        stem = rng.choice(PATRONYM_STEMS)
        return stem + ("en" if stem.endswith("s") else "sen")  # patronymic: Jens->Jensen
    if r < 0.85:
        return rng.choice(TOPO) + rng.choice(TOPO_SUF)  # toponymic compound
    return rng.choice(SURNAMES)  # curated seed


def _given(rng: random.Random) -> str:
    # curated core dominates; 40% from GIVEN_TAIL (full approved list) to defeat memorization
    if GIVEN_TAIL and rng.random() < 0.4:
        return rng.choice(GIVEN_TAIL)
    return rng.choice(GIVEN)


def _person(rng: random.Random) -> str:
    given, surname = _given(rng), _surname(rng)
    r = rng.random()
    if r < 0.12:
        return surname  # surname only
    if r < 0.24:
        return f"{given[0]}. {surname}"  # initialised given
    return f"{given} {surname}"


def _company(rng: random.Random) -> str:
    head = _surname(rng) if rng.random() < 0.5 else rng.choice(COMPANY_WORDS)
    n = 2 if rng.random() < 0.4 else 1
    words = rng.sample(COMPANY_WORDS, min(n, len(COMPANY_WORDS)))
    parts = [head, *words, rng.choice(COMPANY_FORMS)]
    return " ".join(p for p in parts if p)


def junk_text(rng: random.Random) -> str:
    r = rng.random()
    if r < P_NOISE:
        return _noise(rng)
    if r < P_NOISE + P_NUMERIC:
        return _numeric(rng)
    if r < P_NOISE + P_NUMERIC + P_INSTRUCTION:
        return _instruction(rng)
    body = _person(rng) if rng.random() < 0.5 else _company(rng)
    return rng.choice(_PREFIX) + body


def junk_segment(rng: random.Random) -> Segment:
    return Segment(junk_text(rng), "junk")
