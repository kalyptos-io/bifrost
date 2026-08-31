"""in-process aux-map load seam: the small registry-derived branch data, carried per generation.

mirrors StreetIndex/AreaIndex - four unqualified reads off the search_path-bound gen schema, grouped
into the belief-branch maps. one snapshot so a torn read can't build a self-inconsistent context.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg


def _group(pairs: list[tuple[str, str]]) -> dict[str, frozenset[str]]:
    grouped: dict[str, set[str]] = {}
    for name, postcode in pairs:
        grouped.setdefault(name, set()).add(postcode)
    return {name: frozenset(pcs) for name, pcs in grouped.items()}


@dataclass(frozen=True, slots=True)
class AuxMaps:
    city_map: dict[str, frozenset[str]]  # folded city -> postcodes it spans
    subloc_map: dict[str, frozenset[str]]  # folded sub-locality -> postcodes it spans
    postcode_dim: list[str]  # full sorted postcode dimension (includes city-less)

    @classmethod
    def from_rows(
        cls,
        postcode_dim: list[str],
        city_rows: list[tuple[str, str]],
        subloc_rows: list[tuple[str, str]],
    ) -> AuxMaps:
        return cls(
            city_map=_group(city_rows),
            subloc_map=_group(subloc_rows),
            postcode_dim=sorted(postcode_dim),
        )

    @classmethod
    async def load_from(cls, pool: asyncpg.Pool) -> AuxMaps:
        async with (
            pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            dim = await conn.fetch("SELECT postcode FROM aux_postcode_dim")
            cities = await conn.fetch("SELECT folded_name, postcode FROM aux_city_map")
            sublocs = await conn.fetch("SELECT folded_name, postcode FROM aux_subloc_map")
        return cls.from_rows(
            [r["postcode"] for r in dim],
            [(r["folded_name"], r["postcode"]) for r in cities],
            [(r["folded_name"], r["postcode"]) for r in sublocs],
        )
