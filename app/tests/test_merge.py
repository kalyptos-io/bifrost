"""pure belief-merge (Fagin TA) tests - no DB. a FakeAddressSource feeds in-memory rows and counts
fetches so we can assert gated recovery (skipped on settle), the threshold stop, garbage recovery,
judge-only husnr, and the dynamic (access-address vs unit) dedup.
"""

import math

from bifrost.core.merge import _levenshtein, merge, score_row, threshold
from bifrost.core.types import (
    AddressRow,
    Axis,
    Belief,
    Capability,
    Grade,
    Search,
)

_EPS = 1e-3


def _row(
    address_id: str,
    *,
    street_id: int = 0,
    house_number: str = "1",
    postcode: str = "1000",
    house_letter: str | None = None,
    floor: str | None = None,
    door: str | None = None,
    sim: float = 0.0,
    folded_street: str = "x",
    lifecycle: str = "current",
) -> AddressRow:
    return AddressRow(
        address_id=address_id,
        street_id=street_id,
        street=folded_street.upper(),
        folded_street=folded_street,
        house_number=house_number,
        house_letter=house_letter,
        floor=floor,
        door=door,
        postcode=postcode,
        sub_locality=None,
        street_similarity=sim,
        lifecycle=lifecycle,
    )


def _street(value: str = "x") -> Belief:
    return Belief(Axis.STREET, value, 1.0, Grade.TRIGRAM, capability=Capability.SOURCE)


def _husnr(value: str = "1") -> Belief:
    return Belief(Axis.HOUSE_NUMBER, value, 1.0, Grade.HUSNR_FUZZY)  # judge-only


def _postcode(value: str = "1000") -> Belief:
    return Belief(
        Axis.POSTCODE,
        value,
        0.1,
        Grade.POSTCODE_FUZZY,
        capability=Capability.SOURCE,
        members=frozenset({value}),
    )


def _floor(value: str = "1") -> Belief:
    return Belief(Axis.FLOOR, value, 0.4, Grade.UNIT)


def _house_letter(value: str = "a") -> Belief:
    return Belief(Axis.HOUSE_LETTER, value, 0.4, Grade.EXACT)


class FakeAddressSource:
    def __init__(
        self,
        *,
        stream_batches: list[list[AddressRow]] | None = None,
        postcode_rows: list[AddressRow] | None = None,
    ) -> None:
        self._batches = stream_batches or []
        self._postcode_rows = postcode_rows or []
        self.calls = {"street": 0, "postcode": 0}
        self.batches_consumed = 0

    async def street_stream(
        self, folded_q, *, cap, batch, collapse_units=False, postcodes=None, lifecycle
    ):
        self.calls["street"] += 1
        for b in self._batches:
            self.batches_consumed += 1
            yield b

    async def by_postcodes(self, codes, folded_q, house_number, *, cap, lifecycle):
        self.calls["postcode"] += 1
        return self._postcode_rows[:cap]


def test_score_row_matches_the_weighted_log_sum():
    beliefs = (_street(), _husnr("1"), _postcode("1000"), _floor())
    row = _row("a", house_number="1", postcode="1000", floor="1", sim=0.8)
    expected = (  # Σ_core w·ln(ε+belief) + Σ_unit w·match, by hand
        1.0 * math.log(_EPS + 0.8)  # street similarity
        + 1.0 * math.log(_EPS + 1.0)  # husnr equal-length match
        + 0.1 * math.log(_EPS + 1.0)  # postcode digit-grade, distance 0
        + 0.4 * 1.0  # floor value-match, bonus outside the log
    )
    assert math.isclose(score_row(beliefs, row), expected, rel_tol=0, abs_tol=1e-9)


def test_unit_bonus_never_penalizes_absence():
    # a floor belief adds weight only on a value match; an absent or differing unit gets +0, not -w
    beliefs = (_floor("2"),)
    assert score_row(beliefs, _row("a", floor="2")) == 0.4
    assert score_row(beliefs, _row("none", floor=None)) == 0.0
    assert score_row(beliefs, _row("other", floor="3")) == 0.0


async def test_settle_skips_recovery():
    # the husnr probe is fetched as the fast-path check, but its rows are sim 0 < SIM_GATE so the
    # short-circuit gate fails; a perfect street row then settles the stream at depth 1
    beliefs = (_street(), _husnr("1"), _postcode("1000"))
    perfect = _row("hit", sim=0.99, house_number="1", postcode="1000")
    src = FakeAddressSource(stream_batches=[[perfect]], postcode_rows=[_row("pc")])
    res = await merge(Search(beliefs=beliefs), src, k=1)
    assert [c.address_id for c in res] == ["hit"]
    assert src.batches_consumed == 1  # stream not over-deepened past the settle
    assert src.calls["postcode"] == 1  # probe fetched once (gate failed), recovery rows unused


async def test_threshold_keeps_streaming_until_the_true_best():
    # near-tie (husnr mismatch) at depth 1; the real best is a higher-sim match at depth 2.
    # no postcode belief: the husnr short-circuit can't fire, so the stream drives ranking
    beliefs = (_street(), _husnr("1"))
    near = _row("near", street_id=1, sim=0.5, house_number="9")  # husnr mismatch -> penalised
    best = _row("best", street_id=2, sim=0.95, house_number="1")
    src = FakeAddressSource(stream_batches=[[near], [best]])
    res = await merge(Search(beliefs=beliefs), src, k=1)
    assert res[0].address_id == "best"
    assert src.batches_consumed == 2  # did not stop early on the near-tie


async def test_garbage_street_recovers_via_flat_sets():
    beliefs = (_street("zzz"), _husnr("13"), _postcode("6900"))
    garbage = _row("g", street_id=1, sim=0.01, house_number="99", postcode="0000")
    answer = _row("ans", street_id=2, sim=0.0, house_number="13", postcode="6900")
    src = FakeAddressSource(stream_batches=[[garbage]], postcode_rows=[answer])
    res = await merge(Search(beliefs=beliefs), src, k=2)
    assert res[0].address_id == "ans"  # postcode set recovers it; husnr judge out-ranks the garbage
    assert src.calls["postcode"] == 1


def _husnr_letters(letters: str, *, sim: float) -> list[AddressRow]:
    # distinct access addresses sharing husnr "1" at postcode 1000 - >=k of these arm the gate
    return [
        _row(f"1{c}", house_number="1", house_letter=c, postcode="1000", sim=sim) for c in letters
    ]


async def test_husnr_shortcircuit_skips_stream():
    # selective anchor: >=k husnr-match rows on a street at sim >= SIM_GATE answer from the probe
    beliefs = (_street(), _husnr("1"), _postcode("1000"))
    src = FakeAddressSource(
        stream_batches=[[_row("stream", sim=0.99)]], postcode_rows=_husnr_letters("ABCDE", sim=0.95)
    )
    res = await merge(Search(beliefs=beliefs), src, k=5)
    assert len(res) == 5 and "stream" not in [c.address_id for c in res]
    assert src.batches_consumed == 0  # stream never touched
    assert src.calls == {"street": 0, "postcode": 1}


async def test_husnr_shortcircuit_fetches_recovery_once():
    # probe fetched exactly once whether the gate fires or the stream falls through to reuse it
    beliefs = (_street(), _husnr("1"), _postcode("1000"))
    fired = FakeAddressSource(
        stream_batches=[[_row("s", sim=0.99)]], postcode_rows=_husnr_letters("ABCDE", sim=0.95)
    )
    await merge(Search(beliefs=beliefs), fired, k=5)
    assert fired.calls["postcode"] == 1 and fired.batches_consumed == 0  # gate fired, no stream

    # low-sim recovery fails the gate; the stream runs, never settles, and reuses the probe rows
    fell = FakeAddressSource(
        stream_batches=[[_row("s", sim=0.1, house_number="9")]],
        postcode_rows=_husnr_letters("ABCDE", sim=0.1),
    )
    await merge(Search(beliefs=beliefs), fell, k=5)
    assert fell.calls["postcode"] == 1  # reused in fall-through, not re-fetched


async def test_lone_husnr_is_unresolvable_returns_empty():
    # husnr is judge-only and non-selective; with no street/postcode/locality nothing can source
    beliefs = (_husnr("1"),)
    src = FakeAddressSource(stream_batches=[[_row("x")]])
    assert await merge(Search(beliefs=beliefs), src, k=5) == []
    assert src.calls == {"street": 0, "postcode": 0}


async def test_no_source_belief_returns_empty_without_touching_source():
    beliefs = (_floor(), Belief(Axis.HOUSE_LETTER, "b", 0.4, Grade.EXACT))
    src = FakeAddressSource(stream_batches=[[_row("x")]])
    assert await merge(Search(beliefs=beliefs), src, k=5) == []
    assert src.calls == {"street": 0, "postcode": 0}


async def test_dedup_bare_query_collapses_to_access_address():
    # no unit belief: base + its floor/door unit collapse to one access address; base represents
    beliefs = (_street(),)
    base = _row("base", sim=0.9, floor=None, door=None)
    unit = _row("unit", sim=0.9, floor="1", door="tv")  # same access key
    src = FakeAddressSource(stream_batches=[[base, unit]])
    res = await merge(Search(beliefs=beliefs), src, k=5)
    assert len(res) == 1
    assert res[0].address_id == "base" and res[0].floor is None
    assert math.isclose(res[0].score, score_row(beliefs, unit), abs_tol=1e-9)  # max over the group


async def test_unit_query_splits_units_and_lifts_the_match():
    # a floor belief switches dedup to unit granularity; the matching unit gets the bonus and leads
    beliefs = (_street(), _floor("1"))
    base = _row("base", sim=0.9, floor=None, door=None)
    unit = _row("unit", sim=0.9, floor="1", door="tv")
    src = FakeAddressSource(stream_batches=[[base, unit]])
    res = await merge(Search(beliefs=beliefs), src, k=5)
    assert [c.address_id for c in res] == ["unit", "base"]  # split, matching unit ranks first
    assert res[0].floor == "1"


async def test_levenshtein_tiebreak_orders_equal_scores():
    beliefs = (_street("abc"),)
    p = _row("p", street_id=1, sim=0.5, folded_street="abc")  # lev 0
    q = _row("q", street_id=2, sim=0.5, folded_street="abd")  # lev 1 (one sub)
    src = FakeAddressSource(stream_batches=[[q, p]])  # offered q-first; tiebreak must reorder
    res = await merge(Search(beliefs=beliefs), src, k=2)
    assert [c.address_id for c in res] == ["p", "q"]


async def test_candidate_carries_the_presented_lifecycle():
    beliefs = (_street(),)
    src = FakeAddressSource(stream_batches=[[_row("r", sim=0.9, lifecycle="retired")]])
    res = await merge(Search(beliefs=beliefs), src, k=1)
    assert res[0].lifecycle == "retired"  # the row's presented lifecycle rides to the Candidate


async def test_lifecycle_breaks_score_ties_toward_current():
    # same designation-similarity + no earlier tiebreak differs: current outranks retired on the tie
    beliefs = (_street("x"),)
    cur = _row("cur", house_number="1", sim=0.9, folded_street="x", lifecycle="current")
    ret = _row("ret", house_number="2", sim=0.9, folded_street="x", lifecycle="retired")
    src = FakeAddressSource(stream_batches=[[ret, cur]])  # retired offered first
    res = await merge(Search(beliefs=beliefs), src, k=2)
    assert [c.address_id for c in res] == ["cur", "ret"]  # current wins the exact-score tie


async def test_alias_and_canonical_designations_stay_two_candidates():
    # one physical address matched via its current name and a historical alias -> two candidates,
    # each presenting its own designation lifecycle (dedup is per (entity, designation))
    beliefs = (_street("nygade"),)
    canonical = _row("a", sim=0.9, folded_street="nygade", lifecycle="current")
    alias = _row("a", sim=0.9, folded_street="gammelgade", lifecycle="retired")
    src = FakeAddressSource(stream_batches=[[canonical, alias]])
    res = await merge(Search(beliefs=beliefs), src, k=5)
    assert {(c.street, c.lifecycle) for c in res} == {
        ("NYGADE", "current"),
        ("GAMMELGADE", "retired"),
    }


def test_house_letter_match_is_case_insensitive():
    # folded lowercase query vs uppercase db: the letter must match (the "5K" bug), not be penalised
    beliefs = (_house_letter("k"),)
    match = score_row(beliefs, _row("hit", house_letter="K"))
    miss = score_row(beliefs, _row("other", house_letter="G"))
    assert math.isclose(match, 0.4 * math.log(_EPS + 1.0), abs_tol=1e-9)
    assert match > miss


async def test_house_letter_query_ranks_the_matching_letter_first():
    beliefs = (_street(), _house_letter("k"))
    hit = _row("5K", sim=0.9, house_letter="K")
    siblings = [_row(f"5{c}", sim=0.9, house_letter=c) for c in ("G", "E", "H")]
    src = FakeAddressSource(stream_batches=[[*siblings, hit]])  # hit offered last
    res = await merge(Search(beliefs=beliefs), src, k=5)
    assert res[0].address_id == "5K"


async def test_bare_query_prefers_the_letterless_address():
    # no house_letter belief: bare and its lettered siblings tie on score; bare must win the tie
    beliefs = (_street(),)
    bare = _row("31", sim=0.9, house_letter=None)
    siblings = [_row(f"31{c}", sim=0.9, house_letter=c) for c in ("A", "E", "G")]
    src = FakeAddressSource(stream_batches=[[*siblings, bare]])  # bare offered last
    res = await merge(Search(beliefs=beliefs), src, k=5)
    assert res[0].address_id == "31" and res[0].house_letter is None


async def test_letter_query_does_not_prefer_bare():
    # a letter is queried: prefer_bare is off; the matching lettered row leads, bare is penalised
    beliefs = (_street(), _house_letter("a"))
    bare = _row("bare", sim=0.9, house_letter=None)
    hit = _row("hitA", sim=0.9, house_letter="A")
    src = FakeAddressSource(stream_batches=[[bare, hit]])
    res = await merge(Search(beliefs=beliefs), src, k=5)
    assert res[0].address_id == "hitA"


def test_threshold_drops_as_the_street_frontier_falls():
    beliefs = (_street(), _husnr("1"))
    assert threshold(beliefs, 0.9) > threshold(beliefs, 0.2)  # monotone in s_d


def test_locality_scores_a_matching_postcode_over_a_wrong_one():
    # the locality belief scores 1 on a row whose postcode is in its member set, else 0
    city = Belief(Axis.CITY, "skjern", 0.1, Grade.LOCALITY, members=frozenset({"6900"}))
    assert score_row((city,), _row("m", postcode="6900")) > score_row(
        (city,), _row("w", postcode="8000")
    )


def test_husnr_grade_is_equal_length_gated():
    beliefs = (Belief(Axis.HOUSE_NUMBER, "12", 1.0, Grade.HUSNR_FUZZY),)
    exact = score_row(beliefs, _row("a", house_number="12"))  # 1.0
    one_off = score_row(beliefs, _row("b", house_number="13"))  # equal len, lev 1 -> 0.5
    diff_len = score_row(beliefs, _row("c", house_number="1"))  # differing len -> 0.0
    assert exact > one_off > diff_len
    assert math.isclose(one_off, 1.0 * math.log(_EPS + 0.5), abs_tol=1e-9)


def test_levenshtein_matches_pg_parity():
    assert _levenshtein("skjern", "skjern") == 0
    assert _levenshtein("6900", "6905") == 1  # one substitution, default costs
    # asymmetric tiebreak costs, query -> folded_street, matches pg levenshtein(q, fs, 2,3,1)
    assert _levenshtein("ab", "abc", ins=2, dele=3, sub=1) == 2  # insert one target char
    assert _levenshtein("abc", "ab", ins=2, dele=3, sub=1) == 3  # delete one source char
    assert _levenshtein("abc", "abd", ins=2, dele=3, sub=1) == 1  # one substitution
