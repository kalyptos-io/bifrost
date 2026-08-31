"""in-process trigram-KNN area gazetteer over admin_area, off the request path.

mirrors StreetIndex (same shared pg_trgm-parity tokenizer) but ranks names within a single `kind`
(kommune/region/sogn/postcode/city) and adds an exact-code path (postcode). historical area names
(area_alias) union onto the canonical rows, tagged with the alias lifecycle + the canonical area_id;
geometry stays in postgres - the index holds only the small ranking + display fields; the <=k hits'
geometry is fetched per request. emitted order reproduces pg's `folded_name <-> q, area_id`.
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


class AreaHit(NamedTuple):
    area_id: str
    kind: str
    code: str | None
    name: str
    sim: float
    lifecycle: str = "current"


class AreaIndex:
    __slots__ = (
        "_area_id",
        "_kind",
        "_code",
        "_name",
        "_lifecycle",
        "_lifecycle_code",
        "_trglen",
        "_inv",
        "_by_code",
        "_n",
    )

    def __init__(
        self,
        rows: list[tuple[str, str, str | None, str, str]],
        *,
        lifecycles: dict[str, str] | None = None,
        alias_rows: list[tuple[str, str, str, str, str]] | None = None,
    ) -> None:
        # rows: (area_id, kind, code, name, folded_name); lifecycles: canonical area_id->lifecycle;
        # alias_rows: (area_id, kind, name, folded_name, lifecycle) - historical names, no code
        lifecycles = lifecycles or {}
        entries: list[tuple[str, str, str | None, str, str, str]] = [
            (area_id, kind, code, name, folded, lifecycles.get(area_id, "current"))
            for area_id, kind, code, name, folded in rows
        ]
        entries += [
            (area_id, kind, None, name, folded, lifecycle)
            for area_id, kind, name, folded, lifecycle in (alias_rows or ())
        ]
        n = len(entries)
        self._n = n
        self._area_id: list[str] = [""] * n
        self._kind: list[str] = [""] * n
        self._code: list[str | None] = [None] * n
        self._name: list[str] = [""] * n
        self._lifecycle: list[str] = ["current"] * n
        self._trglen = np.empty(n, dtype=np.int32)
        self._by_code: dict[tuple[str, str], list[int]] = {}  # (kind, code) -> positions
        inv: dict[str, list[int]] = {}
        for pos, (area_id, kind, code, name, folded, lifecycle) in enumerate(entries):
            self._area_id[pos] = area_id
            self._kind[pos] = kind
            self._code[pos] = code
            self._name[pos] = name
            self._lifecycle[pos] = lifecycle
            tg = trigrams(folded)
            self._trglen[pos] = len(tg)
            for g in tg:
                inv.setdefault(g, []).append(pos)
            if code is not None:
                self._by_code.setdefault((kind, code), []).append(pos)
        self._inv = {g: np.array(v, dtype=np.int32) for g, v in inv.items()}
        self._lifecycle_code = lifecycle_codes(self._lifecycle)

    def _sims(self, qt: set[str]) -> np.ndarray:
        return bincount_sims(self._inv, self._trglen, self._n, qt)

    def _hit(self, pos: int, sim: float) -> AreaHit:
        return AreaHit(
            self._area_id[pos],
            self._kind[pos],
            self._code[pos],
            self._name[pos],
            sim,
            self._lifecycle[pos],
        )

    def knn(
        self,
        folded_q: str,
        *,
        kind: str,
        cap: int = TOP_K,
        lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
    ) -> list[AreaHit]:
        qt = trigrams(folded_q)
        if not qt:
            return []
        sim = self._sims(qt)
        cand = np.nonzero(sim >= THRESHOLD)[0]
        if cand.size == 0:
            return []
        # kind + lifecycle partition before the cap
        cand = lifecycle_mask(self._lifecycle_code, cand, lifecycle)
        hits = [(int(p), float(sim[p])) for p in cand if self._kind[p] == kind]
        # sim desc, then area_id asc (pg `<-> asc, area_id asc`)
        hits.sort(key=lambda ps: (-ps[1], self._area_id[ps[0]]))
        return [self._hit(pos, s) for pos, s in hits[:cap]]

    def by_code(self, code: str, *, kind: str) -> list[AreaHit]:
        # exact-code lookup (postnummer); sim 1.0 - the code is authoritative, no fuzz. code lives
        # only on canonical rows, so aliases never surface here; caller lifecycle-filters if needed
        return [self._hit(pos, 1.0) for pos in self._by_code.get((kind, code), ())]

    @classmethod
    async def load_from(cls, pool: asyncpg.Pool) -> "AreaIndex":
        # one snapshot: a torn read would build a self-inconsistent gazetteer. the pool search_path
        # pins the generation schema, so these unqualified reads hit the right gen.
        async with (
            pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            rows = await conn.fetch(
                "SELECT area_id, kind, code, name, folded_name, lifecycle FROM admin_area"
            )
            aliases = await conn.fetch(
                "SELECT a.area_id, aa.kind, a.name, a.folded_name, a.lifecycle "
                "FROM area_alias a JOIN admin_area aa USING (area_id)"
            )
        data = [(r["area_id"], r["kind"], r["code"], r["name"], r["folded_name"]) for r in rows]
        lifecycles = {r["area_id"]: r["lifecycle"] for r in rows}
        alias_rows = [
            (r["area_id"], r["kind"], r["name"], r["folded_name"], r["lifecycle"]) for r in aliases
        ]
        return await asyncio.to_thread(cls, data, lifecycles=lifecycles, alias_rows=alias_rows)
