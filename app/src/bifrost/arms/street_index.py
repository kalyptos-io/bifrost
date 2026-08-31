"""in-process trigram-KNN over street_dim, replacing the pg_trgm combo/dim queries on the hot path.

parity with pg_trgm is non-negotiable: per-word '  w ' padding + sliding-3, % prune at THRESHOLD,
emitted order = pg's `<->, street_id, postcode`. fact rows still come from postgres. historical
vejnavne (street_alias) union onto the canonical rows carrying the alias lifecycle + their own
postcode scope; they rank alongside the canonical streets and are lifecycle-filtered before the cap.
"""

import asyncio
from typing import NamedTuple

import asyncpg
import numpy as np

from bifrost.arms._trigram import THRESHOLD, bincount_sims, lifecycle_codes, lifecycle_mask
from bifrost.arms._trigram import similarity as _similarity
from bifrost.arms._trigram import trigrams as _trigrams
from bifrost.core.types import CURRENT_LIFECYCLE


class Combo(NamedTuple):
    """a (street_id, postcode) candidate carrying its street similarity + display.

    alias_lifecycle is the presented designation lifecycle when matched via a historical name, else
    None so the fact row's own lifecycle presents (canonical designation).
    """

    street_id: int
    postcode: str
    sim: float
    street: str
    folded_street: str
    alias_lifecycle: str | None = None


class DimEntry(NamedTuple):
    street: str
    folded_street: str
    sim: float


class StreetRank(NamedTuple):
    """a distinct street designation for the geo feature path: id + sim + display.

    alias_lifecycle is the presented designation lifecycle when matched via a historical name, else
    None so the physical road's own lifecycle presents (canonical designation).
    """

    street_id: int
    sim: float
    street: str
    alias_lifecycle: str | None = None


class StreetIndex:
    __slots__ = (
        "_street_id",
        "_street",
        "_folded",
        "_lifecycle",
        "_lifecycle_code",
        "_is_alias",
        "_entry_pcs",
        "_trglen",
        "_inv",
        "_pos_of",
        "_n",
    )

    def __init__(
        self,
        dim_rows: list[tuple[int, str, str]],
        bridge_rows: list[tuple[int, str]],
        *,
        dim_lifecycles: dict[int, str] | None = None,
        alias_rows: list[tuple[str, str, int, list[str], str]] | None = None,
    ) -> None:
        dim_lifecycles = dim_lifecycles or {}
        alias_rows = alias_rows or []
        # street_id -> postcodes, sorted asc to match pg's `, sp.postcode` combo expansion order
        pcs: dict[int, list[str]] = {}
        for sid, pc in bridge_rows:
            pcs.setdefault(sid, []).append(pc)
        bridge_pcs = {sid: tuple(sorted(v)) for sid, v in pcs.items()}

        n_dim = len(dim_rows)
        n = n_dim + len(alias_rows)
        self._n = n
        self._street_id = np.empty(n, dtype=np.int64)
        self._street: list[str] = [""] * n
        self._folded: list[str] = [""] * n
        self._lifecycle: list[str] = ["current"] * n
        self._is_alias = np.zeros(n, dtype=bool)
        self._entry_pcs: list[tuple[str, ...]] = [()] * n
        self._trglen = np.empty(n, dtype=np.int32)
        self._pos_of: dict[int, int] = {}  # canonical street_id -> position (dims + husnr recovery)
        inv: dict[str, list[int]] = {}

        def _tokenize(pos: int, folded: str) -> None:
            tg = _trigrams(folded)
            self._trglen[pos] = len(tg)
            for g in tg:
                inv.setdefault(g, []).append(pos)

        # canonical first so a sim/street_id tie orders the canonical designation before its aliases
        for pos, (sid, street, folded) in enumerate(dim_rows):
            self._street_id[pos] = sid
            self._street[pos] = street
            self._folded[pos] = folded
            self._lifecycle[pos] = dim_lifecycles.get(sid, "current")
            self._entry_pcs[pos] = bridge_pcs.get(sid, ())
            self._pos_of[sid] = pos
            _tokenize(pos, folded)
        for j, (name, folded, sid, postcodes, lifecycle) in enumerate(alias_rows):
            pos = n_dim + j
            self._street_id[pos] = sid
            self._street[pos] = name
            self._folded[pos] = folded
            self._lifecycle[pos] = lifecycle
            self._is_alias[pos] = True
            self._entry_pcs[pos] = tuple(
                sorted(postcodes)
            )  # scoped to the renamed road (no fan-out)
            _tokenize(pos, folded)
        self._inv = {g: np.array(v, dtype=np.int32) for g, v in inv.items()}
        self._lifecycle_code = lifecycle_codes(self._lifecycle)

    def _sims(self, qt: set[str]) -> np.ndarray:
        return bincount_sims(self._inv, self._trglen, self._n, qt)

    def _ranked_positions(
        self, folded_q: str, lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE
    ) -> tuple[np.ndarray, np.ndarray] | None:
        # candidate positions, sim desc then street_id asc (pg `<-> asc, street_id asc`); lifecycle
        # partitions before the cap so a filtered designation never costs a slot
        qt = _trigrams(folded_q)
        if not qt:
            return None
        sim = self._sims(qt)
        cand = np.nonzero(sim >= THRESHOLD)[0]
        if cand.size == 0:
            return None
        cand = lifecycle_mask(self._lifecycle_code, cand, lifecycle)
        if cand.size == 0:
            return None
        order = cand[np.lexsort((self._street_id[cand], -sim[cand]))]
        return order, sim

    def knn(
        self,
        folded_q: str,
        *,
        cap: int,
        postcodes: set[str] | None = None,
        lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
    ) -> list[Combo]:
        ranked = self._ranked_positions(folded_q, lifecycle)
        if ranked is None:
            return []
        order, sim = ranked
        out: list[Combo] = []
        for p in order:
            pos = int(p)
            sid = int(self._street_id[pos])
            s = float(sim[pos])
            street, folded = self._street[pos], self._folded[pos]
            alias_lc = self._lifecycle[pos] if self._is_alias[pos] else None
            for pc in self._entry_pcs[pos]:  # postcode asc on expansion
                if postcodes is not None and pc not in postcodes:
                    continue
                out.append(Combo(sid, pc, s, street, folded, alias_lc))
                if len(out) >= cap:  # cap counts combos, not streets - matches pg's post-join LIMIT
                    return out
        return out

    def rank_streets(
        self, folded_q: str, *, cap: int, lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE
    ) -> list[StreetRank]:
        """top-cap distinct designations by similarity (geo feature path); no postcode expansion."""
        ranked = self._ranked_positions(folded_q, lifecycle)
        if ranked is None:
            return []
        order, sim = ranked
        return [
            StreetRank(
                int(self._street_id[pos]),
                float(sim[pos]),
                self._street[pos],
                self._lifecycle[pos] if self._is_alias[pos] else None,
            )
            for pos in order[:cap]
        ]

    def dims(self, street_ids: list[int], folded_q: str) -> dict[int, DimEntry]:
        # canonical display for husnr recovery; aliases never resolve here (recovery is canonical)
        qt = _trigrams(folded_q)
        out: dict[int, DimEntry] = {}
        for sid in street_ids:
            pos = self._pos_of.get(sid)
            if pos is None:
                continue
            folded = self._folded[pos]
            sim = _similarity(qt, _trigrams(folded)) if qt else 0.0
            out[sid] = DimEntry(self._street[pos], folded, sim)
        return out

    @classmethod
    async def load_from(cls, pool: asyncpg.Pool) -> "StreetIndex":
        # one snapshot: a torn dim/bridge/alias read would build a self-inconsistent index. the
        # pool search_path pins the generation schema, so these unqualified reads hit the right gen.
        async with (
            pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            dim = await conn.fetch(
                "SELECT street_id, street, folded_street, lifecycle FROM street_dim"
            )
            bridge = await conn.fetch("SELECT street_id, postcode FROM street_postcode")
            alias = await conn.fetch(
                "SELECT name, folded_street, street_id, postcodes, lifecycle FROM street_alias"
            )
        dim_rows = [(r["street_id"], r["street"], r["folded_street"]) for r in dim]
        dim_lifecycles = {r["street_id"]: r["lifecycle"] for r in dim}
        bridge_rows = [(r["street_id"], r["postcode"]) for r in bridge]
        alias_rows = [
            (r["name"], r["folded_street"], r["street_id"], list(r["postcodes"]), r["lifecycle"])
            for r in alias
        ]
        return await asyncio.to_thread(  # tokenization is cpu-bound
            cls, dim_rows, bridge_rows, dim_lifecycles=dim_lifecycles, alias_rows=alias_rows
        )
