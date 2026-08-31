"""aux gazetteers accrued over the address stream: inverted folded city / sub-locality -> postcode
sets, and the postcode dim. ports the retired load._Aux.

build_generation always accrues them and writes them into the three gen-schema aux tables (same
normalizer fold as serving, DAR-sourced) - one coherent addresses+aux version per generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg
from bifrost.arms.normalize import normalize
from bifrost.db import (
    AUX_CITY_MAP_COLUMNS,
    AUX_POSTCODE_DIM_COLUMNS,
    AUX_SUBLOC_MAP_COLUMNS,
)


@dataclass
class Aux:
    city_map: dict[str, set[str]] = field(default_factory=dict)  # folded city -> postcodes
    subloc_map: dict[str, set[str]] = field(default_factory=dict)  # folded subloc -> postcodes
    postcodes: set[str] = field(default_factory=set)  # the postcode dimension


def acc_aux(o: dict, aux: Aux) -> None:
    postcode = o.get("postcode")
    if not postcode:
        return
    aux.postcodes.add(postcode)
    city = o.get("city")
    if city:
        aux.city_map.setdefault(normalize(city), set()).add(postcode)
    subloc = o.get("sub_locality")
    if subloc:
        aux.subloc_map.setdefault(normalize(subloc), set()).add(postcode)


async def write_aux_tables(writer: asyncpg.Connection, schema: str, aux: Aux) -> None:
    """copy the accrued maps into the three gen-schema aux tables; bridge rows one-per-pair. writer
    search_path is the gen schema (matches load_areas), so tables stay unqualified."""
    await writer.copy_records_to_table(
        "aux_postcode_dim",
        records=[(pc,) for pc in sorted(aux.postcodes)],
        columns=AUX_POSTCODE_DIM_COLUMNS,
    )
    await writer.copy_records_to_table(
        "aux_city_map",
        records=[(name, pc) for name, pcs in aux.city_map.items() for pc in pcs],
        columns=AUX_CITY_MAP_COLUMNS,
    )
    await writer.copy_records_to_table(
        "aux_subloc_map",
        records=[(name, pc) for name, pcs in aux.subloc_map.items() for pc in pcs],
        columns=AUX_SUBLOC_MAP_COLUMNS,
    )
    print(f"[+] wrote aux ({len(aux.postcodes)} postcode dim)")
