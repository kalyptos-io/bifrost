"""the one place the status/aktualitet -> lifecycle mapping lives: sql CASE builders the snapshot
splices into its derivations. extract stages raw status; this classifies. a future virkningfra
overrides to preliminary everywhere; an unmapped status falls back on the virkning window (open ->
current, closed -> retired) and is counted separately (report-only).

register vocabularies (verified against the objekttypekatalog + live probe):
- dar livscyklus: 3 gældende, 4 nedlagt, 2 foreløbig, 5 henlagt (1/6 not distributed).
- matriklen text: Gældende, Historisk, Foreløbig, "Ikke gennemført".
- dagi: no status - virkning window is the lifecycle.
- ds stednavne aktualitet: iAnvendelse, historisk.
"""

from __future__ import annotations

from bifrost.core.types import ABANDONED, CURRENT, PRELIMINARY, RETIRED


def _future(vfra: str) -> str:
    # a not-yet-effective row is preliminary regardless of status (iso text -> timestamptz)
    return f"({vfra} IS NOT NULL AND {vfra}::timestamptz > now())"


def _temporal(vtil: str) -> str:
    return f"CASE WHEN {vtil} IS NULL THEN '{CURRENT}' ELSE '{RETIRED}' END"


def dar_case(status: str, vfra: str, vtil: str) -> str:
    return (
        f"CASE WHEN {_future(vfra)} THEN '{PRELIMINARY}' "
        f"WHEN {status} = '3' THEN '{CURRENT}' "
        f"WHEN {status} = '4' THEN '{RETIRED}' "
        f"WHEN {status} = '2' THEN '{PRELIMINARY}' "
        f"WHEN {status} = '5' THEN '{ABANDONED}' "
        f"WHEN {status} IS NULL THEN '{CURRENT}' "
        f"ELSE {_temporal(vtil)} END"
    )


def mat_case(status: str, vfra: str, vtil: str) -> str:
    return (
        f"CASE WHEN {_future(vfra)} THEN '{PRELIMINARY}' "
        f"WHEN lower({status}) = 'gældende' THEN '{CURRENT}' "
        f"WHEN lower({status}) = 'historisk' THEN '{RETIRED}' "
        f"WHEN lower({status}) = 'foreløbig' THEN '{PRELIMINARY}' "
        f"WHEN lower({status}) = 'ikke gennemført' THEN '{ABANDONED}' "
        f"WHEN {status} IS NULL THEN '{CURRENT}' "
        f"ELSE {_temporal(vtil)} END"
    )


def dagi_case(vfra: str, vtil: str) -> str:
    return (
        f"CASE WHEN {_future(vfra)} THEN '{PRELIMINARY}' "
        f"WHEN {vtil} IS NULL THEN '{CURRENT}' ELSE '{RETIRED}' END"
    )


def ds_case(aktualitet: str) -> str:
    return (
        f"CASE WHEN {aktualitet} = 'iAnvendelse' THEN '{CURRENT}' "
        f"WHEN {aktualitet} = 'historisk' THEN '{RETIRED}' "
        f"ELSE '{CURRENT}' END"
    )


# unmapped-status predicates (report-only counts; the CASE above still classifies them by fallback)
def dar_unmapped(status: str) -> str:
    return f"({status} IS NOT NULL AND {status} NOT IN ('2','3','4','5'))"


def mat_unmapped(status: str) -> str:
    return (
        f"({status} IS NOT NULL AND lower({status}) "
        "NOT IN ('gældende','historisk','foreløbig','ikke gennemført'))"
    )


def ds_unmapped(aktualitet: str) -> str:
    return f"({aktualitet} IS NOT NULL AND {aktualitet} NOT IN ('iAnvendelse','historisk'))"
