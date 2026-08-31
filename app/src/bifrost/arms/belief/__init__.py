"""component belief branches - the detachable arms. one callable per address component.

each takes a Decomposition and emits a weighted Belief (or None if its span is absent). a branch
never queries the addresses table and never returns rows - it only declares belief over its axis.
the aux-bound three (postcode/city/sub_locality) are built per generation via their factories.
"""

from .city import build_city
from .floor_door import door, floor
from .house_letter import house_letter
from .house_number import house_number
from .postcode import build_postcode
from .street import street
from .sub_locality import build_sub_locality

__all__ = [
    "street",
    "house_number",
    "house_letter",
    "floor",
    "door",
    "build_postcode",
    "build_city",
    "build_sub_locality",
]
