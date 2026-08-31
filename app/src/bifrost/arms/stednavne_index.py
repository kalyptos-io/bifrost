"""in-process trigram-KNN gazetteer over stednavne (danske stednavne named places).

mirrors StreetIndex (same shared pg_trgm-parity tokenizer + vectorized lexsort) but ranks across the
whole register - place names carry no partition kind and no code path. rows are pre-sorted by
stednavn_id so the position index is a stable id-asc tiebreak (no pg order to mirror; just
deterministic). geometry stays in postgres; the <=k hits' geometry is fetched per request by the pk.
historic skrivemåder are ordinary rows carrying their own lifecycle - no alias table.
"""

import asyncio
from typing import NamedTuple

import asyncpg
import numpy as np

from bifrost.arms._trigram import (
    THRESHOLD,
    bincount_sims,
    lifecycle_codes,
    lifecycle_mask,
    trigrams,
)
from bifrost.core.types import CURRENT_LIFECYCLE, TOP_K


class StednavnHit(NamedTuple):
    stednavn_id: str
    name: str
    type: str
    sim: float
    lifecycle: str = "current"


class StednavneIndex:
    __slots__ = ("_id", "_name", "_type", "_lifecycle", "_lifecycle_code", "_trglen", "_inv", "_n")

    def __init__(self, rows: list[tuple[str, str, str, str, str]]) -> None:
        # rows: (stednavn_id, name, type, folded_name, lifecycle), pre-sorted by stednavn_id
        n = len(rows)
        self._n = n
        self._id: list[str] = [""] * n
        self._name: list[str] = [""] * n
        self._type: list[str] = [""] * n
        self._lifecycle: list[str] = ["current"] * n
        self._trglen = np.empty(n, dtype=np.int32)
        inv: dict[str, list[int]] = {}
        for pos, (sid, name, type_, folded, lifecycle) in enumerate(rows):
            self._id[pos] = sid
            self._name[pos] = name
            self._type[pos] = type_
            self._lifecycle[pos] = lifecycle
            tg = trigrams(folded)
            self._trglen[pos] = len(tg)
            for g in tg:
                inv.setdefault(g, []).append(pos)
        self._inv = {g: np.array(v, dtype=np.int32) for g, v in inv.items()}
        self._lifecycle_code = lifecycle_codes(self._lifecycle)

    def knn(
        self, folded_q: str, *, cap: int = TOP_K, lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE
    ) -> list[StednavnHit]:
        qt = trigrams(folded_q)
        if not qt:
            return []
        sim = bincount_sims(self._inv, self._trglen, self._n, qt)
        cand = np.nonzero(sim >= THRESHOLD)[0]
        if cand.size == 0:
            return []
        # lifecycle-partition before the cap so a filtered row never costs a slot
        cand = lifecycle_mask(self._lifecycle_code, cand, lifecycle)
        if cand.size == 0:
            return []
        # sim desc, then position (== stednavn_id) asc as a stable tiebreak
        order = cand[np.lexsort((cand, -sim[cand]))][:cap]
        return [
            StednavnHit(
                self._id[p], self._name[p], self._type[p], float(sim[p]), self._lifecycle[p]
            )
            for p in order.tolist()
        ]

    @classmethod
    async def load_from(cls, pool: asyncpg.Pool) -> "StednavneIndex":
        # one snapshot: a torn read would build a self-inconsistent gazetteer. the pool search_path
        # pins the generation schema, so this unqualified read hits the right gen.
        async with (
            pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            rows = await conn.fetch(
                "SELECT stednavn_id, name, type, folded_name, lifecycle FROM stednavne"
            )
        # sort by stednavn_id (pk, unique): the position index becomes the stable tiebreak
        data = sorted(
            (r["stednavn_id"], r["name"], r["type"], r["folded_name"], r["lifecycle"]) for r in rows
        )
        return await asyncio.to_thread(cls, data)
