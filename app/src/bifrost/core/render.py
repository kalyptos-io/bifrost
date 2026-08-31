"""the single shared address formatter: resolved components -> danish betegnelse.

one authority for rendering (same parity discipline as the normalizer) so every endpoint and caller
renders an address identically. pure - no I/O.
"""

from .types import Candidate

# "Randersgade 48A, 3. th, 2100 København Ø" - segments joined by ", ", empties dropped
_COMMA = ", "


def render(c: Candidate) -> str:
    number = f"{c.house_number}{c.house_letter or ''}".strip()
    street = f"{c.street} {number}".strip() if c.street else number
    unit = " ".join(p for p in (f"{c.floor}." if c.floor else "", c.door or "") if p)
    locality = " ".join(p for p in (c.postcode, c.city) if p)
    return _COMMA.join(seg for seg in (street, unit, locality) if seg)
