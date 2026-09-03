"""the seam. arms are injected as plain callables; the stateful sources are Protocols."""

from collections.abc import AsyncGenerator, Callable
from typing import Protocol

from .types import AddressRow, AreaGeom, Belief, Decomposition, EjendomGeom, RoadGeom, StednavnGeom

Normalize = Callable[[str], str]
Decompose = Callable[[str], Decomposition]
BeliefBranch = Callable[[Decomposition], Belief | None]


class AddressSource(Protocol):
    """pure fetch over the registry - no scoring. each row carries its street similarity."""

    def street_stream(
        self,
        folded_q: str,
        *,
        cap: int,
        batch: int,
        collapse_units: bool = False,
        postcodes: set[str] | None = None,
        lifecycle: tuple[str, ...],
    ) -> AsyncGenerator[list[AddressRow]]:
        """rows under the trigram-KNN combos, yielded in batches of descending similarity.

        collapse_units pushes access-address dedup into SQL when no belief reads floor/door.
        postcodes restricts the combos to a believed postcode set; None scans every postcode.
        lifecycle is the requested presented-lifecycle set; the index filters combos before the cap
        and the SQL filters rows before caps, so a non-requested lifecycle never enters the pool.
        """
        ...

    async def by_postcodes(
        self,
        codes: set[str],
        folded_q: str | None,
        house_number: str | None,
        *,
        cap: int,
        lifecycle: tuple[str, ...],
    ) -> list[AddressRow]:
        """rows for the (fuzzy-resolved) postcode set, husnr-match then similarity ordered.

        recovery matches by postcode/husnr (canonical designation), so presented = entity lifecycle;
        rows outside the requested set are filtered in SQL before the cap.
        """
        ...


class GeoSource(Protocol):
    """fuzzy/exact gazetteer over streets + dagi areas; each hit carries its geojson geometry."""

    async def street_features(
        self,
        folded_street: str,
        *,
        cap: int,
        postcodes: set[str] | None = None,
        lifecycle: tuple[str, ...],
    ) -> list[RoadGeom]:
        """ranked physical roads, one per road, each with its complete geometry + postcode set.

        postcodes confines to roads touching that set (a pin); None ranks every matching road.
        lifecycle filters ranked street designations before the cap.
        """
        ...

    async def area_by_code(
        self, kind: str, code: str, *, cap: int, lifecycle: tuple[str, ...]
    ) -> list[AreaGeom]:
        """exact-code area lookup (postnummer); the code is authoritative."""
        ...

    async def area_by_name(
        self, kind: str, folded_name: str, *, cap: int, lifecycle: tuple[str, ...]
    ) -> list[AreaGeom]:
        """fuzzy area-name lookup within a kind (kommune/sogn/region)."""
        ...

    async def ejendom_by_code(
        self, code: str, *, cap: int, lifecycle: tuple[str, ...]
    ) -> list[EjendomGeom]:
        """exact property lookup by BFE or ejerlavskode (a digit query hits both); BFE first.
        the ejerlavskode branch surfaces only ground sfe properties."""
        ...

    async def ejendom_by_betegnelse(
        self, folded_name: str, *, cap: int, lifecycle: tuple[str, ...]
    ) -> list[EjendomGeom]:
        """fuzzy property lookup by parcel betegnelse, grouped to one ground sfe per hit; also
        surfaces non-current parcel designations when a non-current lifecycle is requested."""
        ...

    async def stednavne_by_name(
        self, folded_name: str, *, cap: int, lifecycle: tuple[str, ...]
    ) -> list[StednavnGeom]:
        """fuzzy place-name lookup (in-proc trigram KNN); name-only, no code path."""
        ...

    async def areas_by_codes(self, kind: str, codes: list[str]) -> dict[str, AreaGeom]:
        """code -> best AreaGeom, batched: the projection fan-out resolves all hits in one fetch."""
        ...

    async def areas_by_names(self, kind: str, folded_names: list[str]) -> dict[str, AreaGeom]:
        """folded name -> best AreaGeom, batched (city projection): knn per name, one fetch."""
        ...

    async def streets_by_names(
        self, pairs: list[tuple[str, set[str] | None]]
    ) -> dict[str, RoadGeom]:
        """(folded name, postcode pin) -> best RoadGeom, batched: the street projection fan-out."""
        ...

    async def ejendom_by_bfes(self, refs: list[tuple[str, str | None]]) -> dict[str, EjendomGeom]:
        """bfe -> EjendomGeom, batched (projection fan-out). each ref pairs a bfe with the address's
        own parcel (context jordstykke, or None), spliced in place of the representative parcel."""
        ...

    async def address_area_codes(self, address_ids: list[str], kind: str) -> dict[str, str]:
        """address_id -> denormalized column of `kind` (an area code, `ejendom`'s ejendom_bfe, or
        `jordstykke` for the ejendom card's parcel context); absent where unstamped.

        the per-hit join behind area/ejendom projection: codes stay off the merge stream and are
        fetched only for the resolved hits, mirroring per-hit geometry fetch.
        """
        ...
