"""belief merge: Fagin's Threshold Algorithm over per-axis beliefs. pure domain, no I/O.

one graded sorted stream (the street trigram KNN) drives the threshold; every SOURCE recovery set
(fuzzy postcode, lone-locality) is fetched eagerly and unioned into the pool, never suppressed, so a
dead or garbage street still answers. the threshold stops the stream once the top-k is provably
settled. the score is the weighted log-sum, with the unit (floor/door) bonus held outside the log
so an absent unit never penalizes a correct building.
"""

import asyncio
import heapq
import math
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from functools import lru_cache

from .ports import AddressSource
from .types import (
    CURRENT_LIFECYCLE,
    LIFECYCLE_RANK,
    TOP_K,
    AddressRow,
    Axis,
    Belief,
    Candidate,
    Capability,
    Grade,
    Search,
)

EPS = 1e-3  # sharpness knob (mismatch ~ -6.9*w); composition guards score_params.eps == this
_LOG_EPS = math.log(EPS)  # 0/1-axis miss contribution (== log(EPS+0.0)), hoisted off the row loop
_LOG_EPS1 = math.log(EPS + 1.0)  # 0/1-axis hit contribution and the _hi frontier fill
_POSTCODE_LEN = 4  # dk postcode length; the digit-grade denominator
_LEV_MAX = 255  # clamp untrusted query spans before the O(n*m) dp

# street tiebreak: asymmetric costs (ins=2, del=3, sub=1), query -> folded_street
_TIE_INS, _TIE_DEL, _TIE_SUB = 2, 3, 1

# candidate bounds: flats capped so a flat-only query is O(1)
STREET_STREAM_CAP = 64
STREET_BATCH = 16
POSTCODE_CAP = 4_000  # recovery bound; > max husnr-per-postcode (3632) so husnr-match rows all fit
# bare-locality (no street/husnr) ties every row on the locality belief alone; k arbitrary picks,
# so a small cap only bounds the seq-scan. truncates only when matches >> k, never where recall>0.
LOCALITY_CAP = 200
SIM_GATE = 0.92  # short-circuit: believed street must really exist at this postcode+husnr
STREET_PRESENT = 0.6  # max street-sim below this => street absent from believed postcode, widen

# 0/1 value-match axes; EXACT (logged) and UNIT (bonus) share this map, the grade picks log vs bonus
_VALUE_FIELD = {
    Axis.HOUSE_LETTER: "house_letter",
    Axis.FLOOR: "floor",
    Axis.DOOR: "door",
}


@lru_cache(maxsize=100_000)
def _levenshtein(source: str, target: str, *, ins: int = 1, dele: int = 1, sub: int = 1) -> int:
    """edit distance transforming source -> target with per-op costs (matches pg levenshtein).

    cached: the grade path recomputes this per row over a handful of distinct postcode/husnr values.
    """
    s, t = source[:_LEV_MAX], target[:_LEV_MAX]
    prev = list(range(0, (len(t) + 1) * ins, ins))
    for i, sc in enumerate(s, 1):
        cur = [i * dele]
        for j, tc in enumerate(t, 1):
            cur.append(
                min(
                    prev[j] + dele,  # delete source char
                    cur[j - 1] + ins,  # insert target char
                    prev[j - 1] + (0 if sc == tc else sub),  # substitute
                )
            )
        prev = cur
    return prev[-1]


def _husnr_grade(q: str, r: str) -> float:
    """equal-length-gated: q==r -> 1; differing length -> 0 (12 != 13); else 1 - lev/len."""
    if q == r:
        return 1.0
    if len(q) != len(r):
        return 0.0
    return 1 - _levenshtein(q, r) / len(q)


def _belief_value(b: Belief, row: AddressRow) -> float:
    match b.grade:
        case Grade.TRIGRAM:
            return row.street_similarity
        case Grade.EXACT | Grade.UNIT:
            rv = getattr(row, _VALUE_FIELD[b.axis])  # case-insensitive: query is folded, db isn't
            return 1.0 if rv is not None and rv.casefold() == b.value.casefold() else 0.0
        case Grade.HUSNR_FUZZY:
            return _husnr_grade(b.value, row.house_number)
        case Grade.POSTCODE_FUZZY:
            return max(0.0, 1 - _levenshtein(row.postcode, b.value) / _POSTCODE_LEN)
        case Grade.LOCALITY:
            return 1.0 if row.postcode in (b.members or frozenset()) else 0.0
        case _:
            raise AssertionError(f"unhandled grade: {b.grade}")


def score_row(beliefs: tuple[Belief, ...], row: AddressRow) -> float:
    """the joint belief: Σ_core w·ln(ε+belief) + Σ_unit w·match (unit bonus outside the log)."""
    return _plan(beliefs).score(row)


def _hi(b: Belief) -> float:
    """max contribution this axis can add to any row (the NRA fill for unseen rows)."""
    return b.weight if b.grade is Grade.UNIT else b.weight * _LOG_EPS1


def threshold(beliefs: tuple[Belief, ...], s_d: float) -> float:
    """τ(d): best possible score of any address past the street frontier s_d (TA stop bound)."""
    return _plan(beliefs).tau(s_d)


@dataclass(frozen=True, slots=True)
class _ScorePlan:
    """per-request scoring precompute: pre-casefolds the 0/1 value axes so the per-row loop skips
    the re-casefold and the log on those axes. score(row) stays bit-identical to the naive
    per-belief weighted log-sum."""

    beliefs: tuple[Belief, ...]
    folded: tuple[str | None, ...]  # pre-casefolded EXACT/UNIT value, index-aligned; else None

    def score(self, row: AddressRow) -> float:
        total = 0.0
        for b, folded in zip(self.beliefs, self.folded, strict=True):
            grade = b.grade
            if grade is Grade.EXACT:
                rv = getattr(row, _VALUE_FIELD[b.axis])
                hit = rv is not None and rv.casefold() == folded
                total += b.weight * (_LOG_EPS1 if hit else _LOG_EPS)
            elif grade is Grade.UNIT:
                rv = getattr(row, _VALUE_FIELD[b.axis])
                hit = rv is not None and rv.casefold() == folded
                total += b.weight * (1.0 if hit else 0.0)
            elif grade is Grade.LOCALITY:
                hit = row.postcode in (b.members or frozenset())
                total += b.weight * (_LOG_EPS1 if hit else _LOG_EPS)
            else:  # trigram/husnr/postcode: value varies per row, the log stays
                total += b.weight * math.log(EPS + _belief_value(b, row))
        return total

    def tau(self, s_d: float) -> float:
        # belief-order sum keeps the bound bit-identical; per-batch, so precompute buys nothing
        total = 0.0
        for b in self.beliefs:
            total += b.weight * math.log(EPS + s_d) if b.grade is Grade.TRIGRAM else _hi(b)
        return total


def _plan(beliefs: tuple[Belief, ...]) -> _ScorePlan:
    folded = tuple(
        b.value.casefold() if b.grade in (Grade.EXACT, Grade.UNIT) else None for b in beliefs
    )
    return _ScorePlan(beliefs, folded)


class _Agg:
    """per-group rollup: max score over the group, plus the representative row to emit."""

    __slots__ = ("best_score", "rep_row", "rep_rank")

    def __init__(self, score: float, row: AddressRow, rank: tuple) -> None:
        self.best_score = score
        self.rep_row = row
        self.rep_rank = rank


def _is_base(row: AddressRow) -> bool:
    return row.floor is None and row.door is None


class _TopK:
    """dedup to the address granularity before any top-k cut: access address by default, the unit
    (floor/door) when a unit belief is present - the adaptive granularity."""

    def __init__(self, q_street: str | None, *, unit: bool, prefer_bare: bool) -> None:
        self._q_street = q_street
        self._unit = unit
        self._prefer_bare = prefer_bare  # no letter queried: break ties toward the bare address
        self._agg: dict[tuple, _Agg] = {}

    def _key(self, row: AddressRow) -> tuple:
        # folded_street in the key: same address via current + alias names stays two candidates
        key = (row.street_id, row.folded_street, row.house_number, row.postcode, row.house_letter)
        return (*key, row.floor, row.door) if self._unit else key

    def _rank(self, row: AddressRow, score: float) -> tuple:
        # bare query: prefer the floor/door-null base as representative; unit query: best unit wins
        return (score,) if self._unit else (_is_base(row), score)

    def offer(self, row: AddressRow, score: float) -> None:
        key = self._key(row)
        agg = self._agg.get(key)
        if agg is None:
            self._agg[key] = _Agg(score, row, self._rank(row, score))
            return
        if score > agg.best_score:
            agg.best_score = score
        rank = self._rank(row, score)
        if rank > agg.rep_rank:
            agg.rep_row, agg.rep_rank = row, rank

    def kth(self, k: int) -> float:
        if len(self._agg) < k:
            return -math.inf
        return heapq.nlargest(k, (a.best_score for a in self._agg.values()))[-1]

    def result(self, limit: int) -> list[Candidate]:
        aggs = self._agg.values()
        if len(aggs) > limit:  # tiebreak only the band that can reach top-k, not the whole pool
            cutoff = heapq.nlargest(limit, (a.best_score for a in aggs))[-1]
            aggs = [a for a in aggs if a.best_score >= cutoff]
        head = sorted(
            aggs,
            key=lambda a: (
                -a.best_score,
                self._tiebreak(a.rep_row),
                self._letter_rank(a.rep_row),
                LIFECYCLE_RANK.get(
                    a.rep_row.lifecycle, 99
                ),  # current > preliminary > retired > ...
            ),
        )
        return [_to_candidate(a.rep_row, a.best_score) for a in head[:limit]]

    def _tiebreak(self, row: AddressRow) -> int:
        if self._q_street is None:
            return 0
        return _levenshtein(
            self._q_street, row.folded_street, ins=_TIE_INS, dele=_TIE_DEL, sub=_TIE_SUB
        )

    def _letter_rank(self, row: AddressRow) -> int:
        # only on a no-letter query: the bare address outranks its lettered siblings on a score tie
        return 0 if self._prefer_bare and row.house_letter is None else 1


def _to_candidate(row: AddressRow, score: float) -> Candidate:
    return Candidate(
        address_id=row.address_id,
        street=row.street,
        house_number=row.house_number,
        postcode=row.postcode,
        city=row.city or "",  # point-in-time city off the fact row; "" = city-less (absent on wire)
        house_letter=row.house_letter,
        floor=row.floor,
        door=row.door,
        sub_locality=row.sub_locality,
        score=score,
        lifecycle=row.lifecycle,
        # rep_row coords safe: registry bakes a husnummer's identical ap/vp into every unit row
        adgangspunkt_x=row.adgangspunkt_x,
        adgangspunkt_y=row.adgangspunkt_y,
        vejpunkt_x=row.vejpunkt_x,
        vejpunkt_y=row.vejpunkt_y,
    )


def _source_postcodes(by_axis: dict[Axis, Belief]) -> set[str] | None:
    """the believed SOURCE postcode set candidates may be sourced from, else None to scan all."""
    pc = by_axis.get(Axis.POSTCODE)
    if pc is not None and pc.capability is Capability.SOURCE and pc.members:
        return set(pc.members)
    return None


def _husnr_probe(
    by_axis: dict[Axis, Belief],
    source: AddressSource,
    folded_q: str | None,
    lifecycle: tuple[str, ...],
):
    """the SOURCE-postcode recovery coroutine (husnr-filtered when present), or None."""
    postcodes = _source_postcodes(by_axis)
    if postcodes is None:
        return None
    husnr = by_axis.get(Axis.HOUSE_NUMBER)
    return source.by_postcodes(
        postcodes,
        folded_q,
        husnr.value if husnr else None,
        cap=POSTCODE_CAP,
        lifecycle=lifecycle,
    )


def _recovery_fetches(
    by_axis: dict[Axis, Belief],
    source: AddressSource,
    folded_q: str | None,
    lifecycle: tuple[str, ...],
):
    """the eager SOURCE recovery sets, plus locality-as-source on a lone-locality query (B4)."""
    fetches = []
    probe = _husnr_probe(by_axis, source, folded_q, lifecycle)
    if probe is not None:
        fetches.append(probe)

    has_strong = any(b.capability is Capability.SOURCE for b in by_axis.values())
    locality = [b.members for b in by_axis.values() if b.grade is Grade.LOCALITY and b.members]
    # lone city / sub-locality query: its postcode set IS the source (don't double-source otherwise)
    if not has_strong and locality:
        husnr = by_axis.get(Axis.HOUSE_NUMBER)
        hn = husnr.value if husnr else None
        codes = set().union(*locality)
        # bare locality scores every row alike; only street/husnr recovery needs the full cap
        cap = LOCALITY_CAP if folded_q is None and hn is None else POSTCODE_CAP
        fetches.append(source.by_postcodes(codes, folded_q, hn, cap=cap, lifecycle=lifecycle))
    return fetches


async def _drain_stream(
    stream: AsyncGenerator[list[AddressRow]],
    topk: _TopK,
    plan: _ScorePlan,
    k: int,
) -> tuple[list[Candidate] | None, float]:
    """offer rows into topk; return (top-k if the TA settles else None, max street-sim seen)."""
    max_sim = 0.0
    async with aclosing(stream) as batches:
        async for rows in batches:
            if not rows:
                continue
            for row in rows:
                topk.offer(row, plan.score(row))
                max_sim = max(max_sim, row.street_similarity)
            s_d = min(r.street_similarity for r in rows)
            # a settle bounds every unstreamed row too (similarity <= s_d), so recovery is redundant
            if topk.kth(k) >= plan.tau(s_d):
                return topk.result(k), max_sim
    return None, max_sim


async def merge(
    search: Search,
    source: AddressSource,
    *,
    k: int = TOP_K,
    lifecycle: tuple[str, ...] = CURRENT_LIFECYCLE,
) -> list[Candidate]:
    beliefs = search.beliefs
    by_axis = {b.axis: b for b in beliefs}
    has_source = any(b.capability is Capability.SOURCE for b in beliefs)
    has_locality = any(b.grade is Grade.LOCALITY and b.members for b in beliefs)
    if not has_source and not has_locality:
        return []  # nothing can source: don't scan 3.9m

    plan = _plan(beliefs)
    street = by_axis.get(Axis.STREET)
    folded_q = street.value if street else None
    unit = Axis.FLOOR in by_axis or Axis.DOOR in by_axis
    prefer_bare = Axis.HOUSE_LETTER not in by_axis

    # husnr short-circuit: a selective anchor (street+husnr+SOURCE postcode) answers from the ~1ms
    # recovery probe alone, skipping the stream
    husnr_scored: list[tuple[AddressRow, float]] | None = None
    if street is not None and Axis.HOUSE_NUMBER in by_axis and not unit:
        probe = _husnr_probe(by_axis, source, folded_q, lifecycle)
        if probe is not None:
            husnr_rows = await probe
            sc = _TopK(folded_q, unit=unit, prefer_bare=prefer_bare)
            husnr_scored = []
            max_sim = 0.0
            for row in husnr_rows:
                s = plan.score(row)
                sc.offer(row, s)
                husnr_scored.append((row, s))
                max_sim = max(max_sim, row.street_similarity)
            # leader pinned: >=k husnr-matches on a street this similar => position 0 is the
            # stream's too, so recovery alone is recall-exact
            if len(sc._agg) >= k and husnr_rows and max_sim >= SIM_GATE:
                return sc.result(k)

    topk = _TopK(folded_q, unit=unit, prefer_bare=prefer_bare)  # lazy: the short-circuit skips it

    # graded street stream (TA fast-path), confined to the believed postcode when one anchors
    if street is not None:
        pcs = _source_postcodes(by_axis)
        # collapse: no unit belief => an access addr's floor/door rows score alike
        settled, max_sim = await _drain_stream(
            source.street_stream(
                street.value,
                cap=STREET_STREAM_CAP,
                batch=STREET_BATCH,
                collapse_units=not unit,
                postcodes=pcs,
                lifecycle=lifecycle,
            ),
            topk,
            plan,
            k,
        )
        # street absent from the believed postcode => wrong postcode; widen to recover it elsewhere
        if pcs is not None and max_sim < STREET_PRESENT:
            settled, _ = await _drain_stream(
                source.street_stream(
                    street.value,
                    cap=STREET_STREAM_CAP,
                    batch=STREET_BATCH,
                    collapse_units=not unit,
                    lifecycle=lifecycle,
                ),
                topk,
                plan,
                k,
            )
        if settled is not None:
            return settled

    # no settle/street: recovery bounds a dead street / lone-locality query; reuse the scored probe
    # (SOURCE postcode => no locality branch, so it's the whole recovery set)
    if husnr_scored is not None:
        for row, s in husnr_scored:
            topk.offer(row, s)
    else:
        fetches = _recovery_fetches(by_axis, source, folded_q, lifecycle)
        for rows in await asyncio.gather(*fetches) if fetches else ():
            for row in rows:
                topk.offer(row, plan.score(row))

    return topk.result(k)


# abracadabra
