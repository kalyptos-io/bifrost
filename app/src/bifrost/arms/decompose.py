"""decomposer: the NER segmenter is the sole, always-on token router -> component spans. it
segments, it does not denoise. routing is soft: a mis-tag only weakens a clause, never cuts (single
LIMIT on the combined belief)."""

from bifrost.arms.segmenter import segment
from bifrost.core.types import Decomposition

# segmenter span labels that map 1:1 onto a Decomposition field; junk is dropped
_FIELDS = frozenset(
    {
        "street",
        "house_number",
        "house_letter",
        "floor",
        "door",
        "postcode",
        "city",
        "sub_locality",
    }
)


def decompose(text: str) -> Decomposition:
    fields: dict[str, str] = {}
    for label, start, end in segment(text):
        if label not in _FIELDS:  # junk: isolated noise, not a component
            continue
        piece = text[start:end].strip()
        if not piece:
            continue
        fields[label] = f"{fields[label]} {piece}" if label in fields else piece
    return Decomposition(text=text, **fields)
