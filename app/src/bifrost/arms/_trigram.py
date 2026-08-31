"""shared pg_trgm-parity trigram tokenizer for the in-process indexes (street, area).

parity is non-negotiable: the tokenizer matches pg_trgm (per-word '  w ' padding, sliding-3) and
THRESHOLD mirrors the % prune floor, so a numpy bincount over an inverted index reproduces pg's
`folded <-> q` order. one tokenizer, one source of truth - both indexes import it.
"""

import re

import numpy as np

from bifrost.core.types import LIFECYCLE_RANK, LIFECYCLE_VALUES

# % prune floor; mirrors the pg_trgm.similarity_threshold the combo path SET per-acquire
THRESHOLD = 0.1


def lifecycle_codes(lifecycles: list[str]) -> np.ndarray:
    # per-row lifecycle rank; np.isin over int codes replaces a per-request python membership loop
    return np.array([LIFECYCLE_RANK[lc] for lc in lifecycles], dtype=np.int8)


def lifecycle_mask(codes: np.ndarray, cand: np.ndarray, lifecycle: tuple[str, ...]) -> np.ndarray:
    # keep candidate positions in the requested lifecycles; an every-state request skips the mask
    if len(lifecycle) >= len(LIFECYCLE_VALUES):
        return cand
    want = np.array([LIFECYCLE_RANK[lc] for lc in lifecycle], dtype=np.int8)
    return cand[np.isin(codes[cand], want)]


_WORD = re.compile(r"[0-9a-z]+")  # folded text is ascii; pg_trgm words are alnum runs


def trigrams(s: str) -> set[str]:
    out: set[str] = set()
    for w in _WORD.findall(s):
        p = f"  {w} "  # pg_trgm pads each word: 2 leading, 1 trailing, then sliding-3
        for i in range(len(p) - 2):
            out.add(p[i : i + 3])
    return out


def similarity(a: set[str], b: set[str]) -> float:
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter) if inter else 0.0


def bincount_sims(
    inv: dict[str, np.ndarray], trglen: np.ndarray, n: int, qt: set[str]
) -> np.ndarray:
    # |A∩B| per row via bincount over the query trigrams' postings, then |A∩B|/|A∪B|
    parts = [inv[g] for g in qt if g in inv]
    if not parts:
        return np.zeros(n, dtype=np.float64)
    inter = np.bincount(np.concatenate(parts), minlength=n).astype(np.float64)
    denom = len(qt) + trglen - inter
    # denom=|A∪B|>=1 for non-empty qt, so inter/denom is already 0 where inter is 0
    return inter / denom
