"""shared normalizer - THE workhorse. imported by serving AND train/gen; parity is non-negotiable.

stdlib-only leaf module (no intra-app imports) so train can depend on it cheaply. order:
url-decode; utf-8<-latin-1 mojibake repair; lowercase; recipient-line strip;
`. , / -` -> space + collapse whitespace; accent-fold + digraph-fold (ae<->ae, oe<->oe, aa<->aa)
applied to query AND registry. fold() is public: the single source of the fold constant, shared by
train/gen so corruption and canonicalization can never drift; MARKER, PUNCT, url_decode and
repair_mojibake are the same seam (train/gen corrupts through them).
"""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote_plus

# fold before NFKD: NFKD would map å->a not "aa"; æ/ø don't decompose, so the table is the only path
_DIGRAPH = str.maketrans({"æ": "ae", "ø": "oe", "å": "aa", "Æ": "Ae", "Ø": "Oe", "Å": "Aa"})

# c/o + attn markers: a comma-segment carrying one is recipient noise; tail junk is segmenter's job
MARKER = re.compile(r"\bc/?o\b|\batt(?:n|ention)?\b|\bv/")

PUNCT = str.maketrans({".": " ", ",": " ", "/": " ", "-": " "})

# bump when fold()/normalize() output changes: the derived folded_* columns depend on it, so a bump
# is a shape change (folds into the seed fingerprint -> reseed)
NORMALIZER_VERSION = "1"


def fold(s: str) -> str:
    """case-preserving accent + digraph fold; the canonical form query and registry share."""
    s = s.translate(_DIGRAPH)
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def strip_pad(s: str) -> str:
    """drop leading zeros on digit runs (030->30, 01->1); safe - no dk postcode is 0-prefixed."""
    return re.sub(r"\b0+(\d)", r"\1", s)


def url_decode(s: str) -> str:
    if "%" not in s and "+" not in s:  # unquote_plus is identity without % or +
        return s
    for _ in range(4):  # double-encoding (%2520) needs a second pass; cap guards pathological input
        decoded = unquote_plus(s)
        if decoded == s:
            break
        s = decoded
    return s


def repair_mojibake(s: str) -> str:
    # utf-8 shown as latin-1 (Ã¸ -> ø); round-trips only on genuine mojibake, correct text raises
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _strip_recipient(s: str) -> str:
    # marked seg is noise only without address signal; keep digit-bearing ones (tail -> segmenter)
    kept = [seg for seg in s.split(",") if any(c.isdigit() for c in seg) or not MARKER.search(seg)]
    return " ".join(kept) if kept else s  # never strip to empty


def normalize(query: str) -> str:
    s = url_decode(query)
    s = repair_mojibake(s)  # before lowercase: lowercasing Ã->ã corrupts the byte
    s = _strip_recipient(s.lower())
    return strip_pad(" ".join(fold(s.translate(PUNCT)).split()))
