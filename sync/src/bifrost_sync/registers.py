"""declarative register/entity catalog: everything entity-specific is data, not code.

each EntitySpec names a datafordeler register + entity, its canonical staging table, the keep-list
columns (canonical ascii name <- source header / dialect variants / value sniffer, + a Kind), the
currency predicate, and the identity pk. extract.shape_row projects + renames + kind-converts a raw
csv row into the canonical staging shape; reduce folds current rows per pk. nothing downstream sees
dialect spellings - staging names are ascii snake_case.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

DAR = "DAR"
DAGI = "DAGI"
MAT = "MAT"
DS = "DS"
EBR = "EBR"

# global extraction-semantics version; bump when extract code semantics change with no spec-field
# change (contract_hash otherwise sees identical specs and would not force a re-baseline)
EXTRACTION_SEMANTICS_VERSION = 4


class Currency(Enum):
    """which rows are an entity's current state; a delete is a row that fails the predicate."""

    DAR = auto()  # dar livscyklus status 3 (gældende) + temporally open
    MAT = auto()  # matriklen text status "Gældende" + temporally open
    OPEN = auto()  # temporally open only (status is quality/other, not currency)
    AKTUALITET = auto()  # ds bitemporal: aktualitet == iAnvendelse (no til columns)


class Kind(Enum):
    """staging value conversion for a column."""

    TEXT = auto()  # raw string, stripped; empty -> null
    DOUBLE = auto()  # float
    GEOJSON = auto()  # wkt -> compact geojson json string
    POINT_XY = auto()  # wkt point -> (x, y) doubles across two columns
    POINT_TEXT = auto()  # wkt point -> "x y" verbatim
    TIMESTAMP = auto()  # iso timestamp -> canonical utc iso (fold + point-in-time joins compare it)


# a value sniffer resolves an undocumented header by inspecting a populated row's values
Sniffer = Callable[[dict], "str | None"]


def poly_col(r: dict) -> str | None:
    return next(
        (
            k
            for k, v in r.items()
            if v and v.lstrip().upper().startswith(("POLYGON", "MULTIPOLYGON"))
        ),
        None,
    )


def line_col(r: dict) -> str | None:
    return next(
        (
            k
            for k, v in r.items()
            if v and v.lstrip().upper().startswith(("LINESTRING", "MULTILINESTRING"))
        ),
        None,
    )


def point_col(r: dict) -> str | None:
    return next((k for k, v in r.items() if v and v.lstrip()[:5].upper() == "POINT"), None)


def ejerlav_kode_col(r: dict) -> str | None:
    # ejerlavskode header spelling unverified; an ejerlav*kode header (landsejerlavskode)
    return next((k for k in r if "ejerlav" in k.lower() and "kode" in k.lower()), None)


@dataclass(frozen=True, slots=True)
class Column:
    name: str | tuple[str, str]  # canonical staging column(s); a 2-tuple only for POINT_XY (x, y)
    # source header, a tuple of dialect variants (first present wins), or a value sniffer
    src: str | tuple[str, ...] | Sniffer
    kind: Kind = Kind.TEXT


@dataclass(frozen=True, slots=True)
class EntitySpec:
    register: str
    entity: str
    table: str
    columns: tuple[Column, ...]
    currency: Currency
    pk: str = "id_lokalId"  # source identity header; reduce folds on row[pk]
    muni_split_totals: bool = False
    delta_retention_days: int = 14
    type_label: str | None = None  # ds geometry entities: wire type of the places they name
    # version-history spec: composite dlt key = pk_out + these; a version pass-through, not a fold
    version_key: tuple[str, ...] = ()
    baseline_variant: str = (
        "current"  # "current" | "bitemporal"; the national total to baseline from
    )

    @property
    def pk_out(self) -> str:
        # canonical staging name of the identity column (dlt merge key + tombstone key)
        return next(c.name for c in self.columns if c.src == self.pk)  # type: ignore[return-value]

    @property
    def is_hist(self) -> bool:
        return bool(self.version_key)

    @property
    def merge_key(self) -> str | list[str]:
        # dlt primary_key: pk_out alone for a folded table, a composite for a version-history table
        return [self.pk_out, *self.version_key] if self.version_key else self.pk_out

    @property
    def download_name(self) -> str:
        # a hist spec shares its entity's downloads; a distinct basename keeps zips + prune apart
        return f"{self.entity}_hist" if self.is_hist else self.entity


_ID = Column("id", "id_lokalId")

# lifecycle columns staged for the snapshot's status->lifecycle CASE (extract classifies, never
# filters). status/aktualitet is the discriminator; virkningfra drives the future->preliminary
# override. registrering* stage only on hist specs (the composite key + point-in-time selection).
_STATUS = Column("status", "status")
_AKTUALITET = Column("aktualitet", "aktualitet")
_VFRA = Column("virkningfra", "virkningFra", Kind.TIMESTAMP)
_VTIL = Column("virkningtil", "virkningTil", Kind.TIMESTAMP)
_RFRA = Column("registreringfra", "registreringFra", Kind.TIMESTAMP)
_RTIL = Column("registreringtil", "registreringTil", Kind.TIMESTAMP)
_LIFECYCLE = (_STATUS, _VFRA, _VTIL)


_DAR = (
    EntitySpec(
        DAR,
        "NavngivenVej",
        "dar_navngivenvej",
        (
            _ID,
            Column("vejnavn", "vejnavn"),
            Column("geometry", line_col, Kind.GEOJSON),
            *_LIFECYCLE,
        ),
        Currency.DAR,
    ),
    EntitySpec(
        DAR,
        "Postnummer",
        "dar_postnummer",
        (_ID, Column("postnr", "postnr"), Column("navn", "navn")),
        Currency.DAR,
    ),
    EntitySpec(
        DAR,
        "SupplerendeBynavn",
        "dar_supplerendebynavn",
        (_ID, Column("navn", "navn")),
        Currency.DAR,
    ),
    # adressepunkt status (8/9) is redundant with the husnummer lifecycle; kept as geometry only
    EntitySpec(
        DAR,
        "Adressepunkt",
        "dar_adressepunkt",
        (_ID, Column(("x", "y"), point_col, Kind.POINT_XY)),
        Currency.OPEN,
    ),
    EntitySpec(
        DAR,
        "Husnummer",
        "dar_husnummer",
        (
            _ID,
            Column("husnummertekst", "husnummertekst"),
            Column("navngivenvej", "navngivenVej"),
            Column("postnummer", "postnummer"),
            Column("supplerende_bynavn", "supplerendeBynavn"),
            Column("adgangspunkt", "adgangspunkt"),
            Column("vejpunkt", "vejpunkt"),
            Column("kommuneinddeling", "kommuneinddeling"),
            Column("sogneinddeling", "sogneinddeling"),
            Column("jordstykke", "jordstykke"),
            *_LIFECYCLE,
        ),
        Currency.DAR,
    ),
    EntitySpec(
        DAR,
        "Adresse",
        "dar_adresse",
        (
            _ID,
            Column("husnummer", "husnummer"),
            Column("etage", "etagebetegnelse"),
            # door: ascii doerbetegnelse (current-total) or dørbetegnelse (bitemporal/delta)
            Column("door", ("doerbetegnelse", "dørbetegnelse")),
            *_LIFECYCLE,
        ),
        Currency.DAR,
    ),
)


# thin name-history siblings (composite dlt key, no geometry): baseline from the bitemporal total so
# street-rename + old-postdistrikt history is present day one. reduce is a version pass-through.
_DAR_HIST = (
    EntitySpec(
        DAR,
        "NavngivenVej",
        "dar_navngivenvej_hist",
        (_ID, Column("vejnavn", "vejnavn"), _STATUS, _VFRA, _VTIL, _RFRA, _RTIL),
        Currency.DAR,
        version_key=("registreringfra", "virkningfra"),
        baseline_variant="bitemporal",
    ),
    EntitySpec(
        DAR,
        "Postnummer",
        "dar_postnummer_hist",
        (
            _ID,
            Column("postnr", "postnr"),
            Column("navn", "navn"),
            _STATUS,
            _VFRA,
            _VTIL,
            _RFRA,
            _RTIL,
        ),
        Currency.DAR,
        version_key=("registreringfra", "virkningfra"),
        baseline_variant="bitemporal",
    ),
)


# dagi: all generalization scales kept (the snapshot picks one per code via id_namespace); geometry
# sniffed (header undocumented). code is the entity's own code field, renamed to a uniform `code`.
def _dagi(entity: str, code_field: str, *, extra: tuple[Column, ...] = ()) -> EntitySpec:
    # no status; dagi lifecycle is virkning-based (virkningfra>now -> preliminary, virkningtil set
    # -> retired). current totals ship only virkning-open rows, so the main table is current-only.
    return EntitySpec(
        DAGI,
        entity,
        f"dagi_{entity.lower()}",
        (
            _ID,
            Column("navn", "navn"),
            Column("code", code_field),
            Column("id_namespace", "id_namespace"),
            Column("geometry", poly_col, Kind.GEOJSON),
            _VFRA,
            _VTIL,
            *extra,
        ),
        Currency.OPEN,
        delta_retention_days=28,
    )


_DAGI = (
    # kommune carries the region ref (no regionskode of its own); totals spell it regionLokalid,
    # deltas regionLokalId - a miss stages all-null and empties every region projection
    _dagi(
        "Kommuneinddeling",
        "kommunekode",
        extra=(Column("region_lokalid", ("regionLokalid", "regionLokalId")),),
    ),
    _dagi("Regionsinddeling", "regionskode"),
    _dagi("Sogneinddeling", "sognekode"),
    _dagi("Postnummerinddeling", "postnummer"),
    _dagi("Retskreds", "retskredsnummer"),
    _dagi("Politikreds", "politikredsnummer"),
    _dagi("Opstillingskreds", "opstillingskredsnummer"),
)


_MAT = (
    EntitySpec(
        MAT,
        "SamletFastEjendom",
        "mat_samletfastejendom",
        (_ID, Column("bfe", "BFEnummer"), *_LIFECYCLE),
        Currency.MAT,
        muni_split_totals=True,
    ),
    EntitySpec(
        MAT,
        "Jordstykke",
        "mat_jordstykke",
        (
            _ID,
            Column("samletfastejendom_lokalid", "samletFastEjendomLokalId"),
            Column("ejerlav_lokalid", "ejerlavLokalId"),
            Column("matrikelnummer", "matrikelnummer"),
            # national deltas carry no file context; kommuneLokalId holds the 4-digit code directly
            Column("kommunekode", "kommuneLokalId"),
            *_LIFECYCLE,
        ),
        Currency.MAT,
        muni_split_totals=True,
    ),
    EntitySpec(
        MAT,
        "Ejerlav",
        "mat_ejerlav",
        # ejerlavskode header sniffed; snapshot falls back to an all-digit id when absent
        (_ID, Column("ejerlavskode", ejerlav_kode_col), Column("ejerlavsnavn", "ejerlavsnavn")),
        Currency.MAT,
        muni_split_totals=True,
    ),
    # one centroide per parcel (last current wins); keyed by the parcel it centres, not its own id
    EntitySpec(
        MAT,
        "Centroide",
        "mat_centroide",
        (
            Column("jordstykke", "jordstykkeLokalId"),
            Column("centroid", "geometri", Kind.POINT_TEXT),
        ),
        Currency.MAT,
        pk="jordstykkeLokalId",
        muni_split_totals=True,
    ),
    # many lodflader per parcel (snapshot merges them into one polygon), so each keeps its own id
    EntitySpec(
        MAT,
        "Lodflade",
        "mat_lodflade",
        (
            _ID,
            Column("jordstykke", "jordstykkeLokalId"),
            Column("geometry", "geometri", Kind.GEOJSON),
        ),
        Currency.MAT,
        muni_split_totals=True,
    ),
    # bfe-numbered property types; ship national current csvs (no muni split), unlike the five above
    EntitySpec(
        MAT,
        "Ejerlejlighed",
        "mat_ejerlejlighed",
        (
            _ID,
            Column("bfe", "BFEnummer"),
            Column("ejerlejlighedsnummer", "ejerlejlighedsnummer"),
            Column("sfe_lokalid", "samletFastEjendomLokalId"),
            # register-truncated 30-char headers
            Column("bpfg_punkt_lokalid", "b_BygningPaaFremmedGrundPunktL"),
            Column("bpfg_flade_lokalid", "b_BygningPaaFremmedGrundFladeL"),
            *_LIFECYCLE,
        ),
        Currency.MAT,
    ),
    EntitySpec(
        MAT,
        "BygningPaaFremmedGrundPunkt",
        "mat_bygningpaafremmedgrundpunkt",
        (
            _ID,
            Column("bfe", "BFEnummer"),
            Column("sfe_lokalid", "samletFastEjendomLokalId"),
            *_LIFECYCLE,
        ),
        Currency.MAT,
    ),
    EntitySpec(
        MAT,
        "BygningPaaFremmedGrundFlade",
        "mat_bygningpaafremmedgrundflade",
        (
            _ID,
            Column("bfe", "BFEnummer"),
            Column("sfe_lokalid", "samletFastEjendomLokalId"),
            *_LIFECYCLE,
        ),
        Currency.MAT,
    ),
)


# ds has no id_lokalId - objectid is the identity. Stednavn (names) is bitemporal (aktualitet only,
# no til cols); the geometry entities carry a wire type label + a `geometri` polygon/line/point.
# aktualitet staged (not filtered): historisk skrivemåder are distinct objectids served as retired
# stednavne rows. baseline is bitemporal (no Current csv upstream); each objectid appears once.
_DS_NAME = EntitySpec(
    DS,
    "Stednavn",
    "ds_stednavn",
    (
        Column("objectid", "objectid"),
        Column("skrivemaade", "skrivemaade"),
        Column("navngivetsted_objectid", "navngivetSted_objectid"),
        _AKTUALITET,
    ),
    Currency.AKTUALITET,
    pk="objectid",
    baseline_variant="bitemporal",
)

# ascii-folded entity names as the api spells them -> the wire type of their named places
_DS_GEOM_LABELS = {
    "Bebyggelse": "bebyggelse",
    "Soe": "sø",
    "Vandloeb": "vandløb",
    "Farvand": "farvand",
    "UrentFarvand": "farvand",
    "Havnebassin": "havnebassin",
    "Landskabsform": "landskabsform",
    "Naturareal": "naturareal",
    "Restriktionsareal": "restriktionsareal",
    "Terraenkontur": "terrænkontur",
    "Vej": "vej",
    "Rute": "rute",
    "Jernbane": "jernbane",
    "Standsningssted": "standsningssted",
    "FaergeruteLinje": "færgerute",
    "FaergerutePunkt": "færgerute",
    "Bygning": "bygning",
    "Sevaerdighed": "seværdighed",
    "Fortidsminde": "fortidsminde",
    "Begravelsesplads": "begravelsesplads",
    "Campingplads": "campingplads",
    "Friluftsbad": "friluftsbad",
    "Idraetsanlaeg": "idrætsanlæg",
    "Lufthavn": "lufthavn",
    "Navigationsanlaeg": "navigationsanlæg",
    "AndenTopografiFlade": "topografi",
    "AndenTopografiPunkt": "topografi",
    # raw user-submitted names (lower quality); kept by default
    "UbearbejdetNavnFlade": "ubearbejdet",
    "UbearbejdetNavnLinje": "ubearbejdet",
    "UbearbejdetNavnPunkt": "ubearbejdet",
}


def _ds_geom(entity: str, label: str) -> EntitySpec:
    return EntitySpec(
        DS,
        entity,
        f"ds_{entity.lower()}",
        (Column("objectid", "objectid"), Column("geometry", "geometri", Kind.GEOJSON)),
        Currency.OPEN,
        pk="objectid",
        type_label=label,
    )


_DS = (_DS_NAME, *(_ds_geom(e, label) for e, label in _DS_GEOM_LABELS.items()))


# betegnelse/husnummerLokalId deliberately skipped: unused, keeps the 2.69M-row staging thin
_EBR = (
    EntitySpec(
        EBR,
        "Ejendomsbeliggenhed",
        "ebr_ejendomsbeliggenhed",
        (
            _ID,
            Column("bfe", "bestemtFastEjendomBFENr"),
            Column("adresse_lokalid", "adresseLokalId"),
        ),
        Currency.MAT,
    ),
)


ALL_ENTITIES: tuple[EntitySpec, ...] = (*_DAR, *_DAR_HIST, *_DAGI, *_MAT, *_DS, *_EBR)


# contract hash: a stable per-entity fingerprint of the extraction semantics. structural drift is
# dlt schema-contract's job (frozen columns/types); this catches semantic changes that leave the
# destination columns unchanged (a re-mapped source header, a flipped currency rule, a new sniffer).


def _src_contract(src: str | tuple[str, ...] | Sniffer) -> object:
    # a sniffer's identity is its qualified name (module-level fn, stable across processes)
    if callable(src):
        return {"sniffer": src.__qualname__}
    if isinstance(src, tuple):
        return {"variants": list(src)}
    return {"header": src}


def _column_contract(c: Column) -> dict:
    name = list(c.name) if isinstance(c.name, tuple) else c.name
    return {"name": name, "src": _src_contract(c.src), "kind": c.kind.name}


def contract_hash(spec: EntitySpec) -> str:
    """sha256 over a canonical json view of the extraction contract. enums by name, sniffers by
    qualname - no repr() of objects, so the digest is identical across processes and machines."""
    payload = {
        "extraction_semantics_version": EXTRACTION_SEMANTICS_VERSION,
        "register": spec.register,
        "entity": spec.entity,
        "table": spec.table,
        "columns": [_column_contract(c) for c in spec.columns],
        "currency": spec.currency.name,
        "pk": spec.pk,
        "pk_out": spec.pk_out,
        "muni_split_totals": spec.muni_split_totals,
        "delta_retention_days": spec.delta_retention_days,
        "type_label": spec.type_label,
        "version_key": list(spec.version_key),
        "baseline_variant": spec.baseline_variant,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
