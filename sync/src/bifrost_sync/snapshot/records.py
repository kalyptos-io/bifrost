"""pure ports from the retired load.py/registry.py: coercion, dense street ids, husnr split, the
addresses COPY tuple, and the register-time promotion gates (count floors, prior-generation shrink,
quality ratios). no I/O - the snapshot drives these.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache

from bifrost.arms.normalize import normalize
from bifrost.core.types import LIFECYCLE_RANK
from bifrost.db.generations import Generation

from ..extract import to_float
from .lifecycle import CURRENT

# dar husnr is digits + optional letter ("41A"); the schema keeps number and letter apart
_HUSNR = re.compile(r"^(\d+)([A-Za-z]?)$")


def split_husnr(husnr: str) -> tuple[str | None, str | None]:
    s = husnr.strip()
    m = _HUSNR.match(s)
    if not m:
        return (s or None, None)
    return (m.group(1) or None, m.group(2) or None)


def _s(v: object) -> str | None:
    return None if v is None or v == "" else str(v)


@cache
def _fold_street(street: str) -> str:
    # the one folder, shared with serving via normalize(); cache: streets repeat heavily across rows
    return normalize(street)


class StreetIds:
    """folded street -> dense int (first-seen, stable for stream order); keeps the raw display + the
    best lifecycle seen across the name's addresses (a name is current if any current address bears
    it; its retired-only rows collapse to a retired street_dim entry)."""

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}
        self._dim: list[list] = []  # [id, raw street, folded, lifecycle]

    def id_for(self, street: str, folded: str, lifecycle: str = CURRENT) -> int:
        i = self._ids.get(folded)
        if i is None:
            i = len(self._ids)
            self._ids[folded] = i
            self._dim.append([i, street, folded, lifecycle])
        # keep the best lifecycle seen across the name's addresses
        elif LIFECYCLE_RANK.get(lifecycle, 99) < LIFECYCLE_RANK.get(self._dim[i][3], 99):
            self._dim[i][3] = lifecycle
        return i

    def dim_records(self) -> list[tuple[int, str, str, str]]:
        return [tuple(d) for d in self._dim]  # type: ignore[misc]

    def lookup(self, folded: str) -> int | None:
        return self._ids.get(folded)  # no minting: geom for an address-less street is an orphan

    def __len__(self) -> int:
        return len(self._ids)


def to_record(o: dict, ids: StreetIds) -> tuple | None:
    """one addresses COPY tuple (ADDRESS_COLUMNS order); None to skip rows missing street/husnr."""
    street = _s(o.get("street_name"))
    husnr = _s(o.get("house_number"))
    if not street or not husnr:
        return None
    lifecycle = o.get("lifecycle") or CURRENT
    return (
        o["id"],
        ids.id_for(street, _fold_street(street), lifecycle),
        _s(o.get("postcode")),
        husnr,
        _s(o.get("house_letter")),
        _s(o.get("floor")),
        _s(o.get("door")),
        _s(o.get("sub_locality")),
        to_float(o.get("adgangspunkt_x")),
        to_float(o.get("adgangspunkt_y")),
        to_float(o.get("vejpunkt_x")),
        to_float(o.get("vejpunkt_y")),
        _s(o.get("kommunekode")),
        _s(o.get("regionskode")),
        _s(o.get("sognekode")),
        _s(o.get("retskredsnummer")),
        _s(o.get("politikredsnummer")),
        _s(o.get("opstillingskredsnummer")),
        _s(o.get("jordstykke")),
        _s(o.get("ejendom_bfe")),
        _s(o.get("city")),
        lifecycle,
    )


@dataclass(frozen=True, slots=True)
class Floors:
    """register-time completeness floors (replace the retired seed gate); overridable per CLI.

    gate the CURRENT-lifecycle counts so their calibrated meaning survives the lifecycle rework (a
    retired/preliminary row must never let a short current build register). per-type ejendom gates
    too: a release where a new property type silently misses must not register. floors are
    first-guess conservative - recalibrate after the first real build.

    the ratio gates below sit beside the absolute floors: they catch the losses a floor can't see -
    a generation that shrank against its predecessor, a stream that skipped rows, a source column
    that went all-null (the regionskode 0% hole), or a lifecycle mapping that stopped matching."""

    addresses: int = 3_500_000
    areas: int = 3_000
    matrikel: int = 2_000_000
    stednavne: int = 100_000
    ejendom: int = 2_300_000
    sfe: int = 2_000_000  # default-only, no cli override
    ejerlejlighed: int = 300_000
    bpfg: int = 30_000
    ebr_stamped: int = 200_000  # unit addresses stamped via ebr
    aux_postcode_dim: int = 1_000  # ~1150 dk postcodes; a mass aux drop must not register
    max_shrink: float = 0.02  # per-table drop vs the prior generation
    max_skipped: float = 0.005  # address rows dropped by to_record
    max_null: float = 0.10  # null share of a projection column
    max_unmapped: float = 0.05  # staging rows whose lifecycle status fell back to temporal


@dataclass(frozen=True, slots=True)
class Counts:
    # current-lifecycle counts, gated by Floors
    addresses: int
    areas: int
    matrikel: int
    stednavne: int
    ejendom: int
    sfe: int
    ejerlejlighed: int
    bpfg: int
    ebr_stamped: int
    aux_postcode_dim: int


def floor_violations(counts: Counts, floors: Floors) -> list[str]:
    """the tables that came up short; a non-empty list aborts the build before register()."""
    checks = (
        ("addresses", counts.addresses, floors.addresses),
        ("admin_area", counts.areas, floors.areas),
        ("matrikel", counts.matrikel, floors.matrikel),
        ("stednavne", counts.stednavne, floors.stednavne),
        ("ejendom", counts.ejendom, floors.ejendom),
        ("ejendom.samlet_fast_ejendom", counts.sfe, floors.sfe),
        ("ejendom.ejerlejlighed", counts.ejerlejlighed, floors.ejerlejlighed),
        ("ejendom.bygning_paa_fremmed_grund", counts.bpfg, floors.bpfg),
        ("ebr_stamped_addresses", counts.ebr_stamped, floors.ebr_stamped),
        ("aux_postcode_dim", counts.aux_postcode_dim, floors.aux_postcode_dim),
    )
    return [f"{name} {n} < {floor}" for name, n, floor in checks if n < floor]


def shrink_violations(gen: Generation, prior: Generation | None, floors: Floors) -> list[str]:
    """tables that lost more than max_shrink against the previous generation. absolute floors are
    coarse (3.5M passes a 3.9M register); a 400k drop only shows against the predecessor."""
    if prior is None:
        return []
    checks = (
        ("addresses", gen.row_count, prior.row_count),
        ("admin_area", gen.area_count, prior.area_count),
        ("matrikel", gen.matrikel_count, prior.matrikel_count),
        ("stednavne", gen.stednavne_count, prior.stednavne_count),
        ("ejendom", gen.ejendom_count, prior.ejendom_count),
    )
    return [
        f"{name} {n} shrank {(before - n) / before:.1%} vs {prior.schema_name} ({before})"
        for name, n, before in checks
        if before and n < before * (1 - floors.max_shrink)
    ]


def ratio_violations(counts: Mapping[str, tuple[int, int]], limit: float) -> list[str]:
    """labels whose bad/total share exceeds `limit`; an empty population can't violate."""
    return [
        f"{label} {bad}/{total} = {bad / total:.1%} > {limit:.1%}"
        for label, (bad, total) in counts.items()
        if total and bad / total > limit
    ]
