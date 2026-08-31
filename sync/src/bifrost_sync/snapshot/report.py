"""integrity signals logged before register(): per-lifecycle counts, alias counts, missing geometry
(non-current), unmapped statuses, adresse/husnummer lifecycle mismatch, and the per-register history
horizon (first bitemporal registration epoch). the calibrated Floors gate the current counts; the
lifecycle signals return as {label: (bad, total)} so the build gates their ratio too.
"""

from __future__ import annotations

import asyncpg

from . import STAGING
from .lifecycle import dar_case, dar_unmapped, ds_unmapped, mat_unmapped
from .read import has_table

_LIFECYCLE_TABLES = (
    "addresses",
    "street_dim",
    "road",
    "admin_area",
    "matrikel",
    "stednavne",
    "ejendom",
)
_GEOMETRY_TABLES = ("road", "admin_area", "stednavne", "matrikel")

# staging table -> unmapped-status predicate (a status the CASE classified by temporal fallback)
_UNMAPPED = {
    "dar_navngivenvej": dar_unmapped("status"),
    "dar_husnummer": dar_unmapped("status"),
    "dar_adresse": dar_unmapped("status"),
    "mat_jordstykke": mat_unmapped("status"),
    "mat_samletfastejendom": mat_unmapped("status"),
    "mat_ejerlejlighed": mat_unmapped("status"),
    "ds_stednavn": ds_unmapped("aktualitet"),
}
_HIST_TABLES = ("dar_navngivenvej_hist", "dar_postnummer_hist")


async def log_report(
    reader: asyncpg.Connection,
    writer: asyncpg.Connection,
    schema: str,
    aliases: tuple[int, int],
    *,
    staging: str = STAGING,
) -> dict[str, tuple[int, int]]:
    """logs every signal; returns the gateable ones as {label: (bad, total)}."""
    lifecycle: dict[str, tuple[int, int]] = {}
    for t in _LIFECYCLE_TABLES:
        rows = await writer.fetch(
            f"SELECT lifecycle, count(*) AS n FROM {t} GROUP BY lifecycle ORDER BY lifecycle"
        )
        breakdown = ", ".join(f"{r['lifecycle']}={r['n']}" for r in rows)
        print(f"[i] {t} lifecycle: {breakdown or 'empty'}")

    print(f"[i] aliases: street={aliases[0]}, area={aliases[1]}")

    for t in _GEOMETRY_TABLES:
        miss = await writer.fetchval(
            f"SELECT count(*) FROM {t} WHERE geometry IS NULL AND lifecycle <> 'current'"
        )
        if miss:
            print(f"[i] {t}: {miss} non-current rows without geometry")

    for table, predicate in _UNMAPPED.items():
        if not await has_table(reader, f"{staging}.{table}"):
            continue
        row = await reader.fetchrow(
            f"SELECT count(*) FILTER (WHERE {predicate}) AS bad, count(*) AS total "
            f'FROM "{staging}".{table}'
        )
        lifecycle[f"{table}.unmapped_status"] = (row["bad"], row["total"])
        if row["bad"]:
            print(
                f"[!] {table}: {row['bad']} rows with an unmapped status "
                "(classified by temporal fallback)"
            )

    if await has_table(reader, f"{staging}.dar_adresse") and await has_table(
        reader, f"{staging}.dar_husnummer"
    ):
        a, h = (
            dar_case("a.status", "a.virkningfra", "a.virkningtil"),
            dar_case("h.status", "h.virkningfra", "h.virkningtil"),
        )
        row = await reader.fetchrow(
            f"SELECT count(*) FILTER (WHERE ({a}) <> ({h})) AS bad, count(*) AS total "
            f'FROM "{staging}".dar_adresse a '
            f'JOIN "{staging}".dar_husnummer h ON h.id = a.husnummer '
            "WHERE a._deleted IS NOT TRUE AND h._deleted IS NOT TRUE"
        )
        lifecycle["adresse_husnummer_lifecycle"] = (row["bad"], row["total"])
        if row["bad"]:
            print(f"[!] {row['bad']} addresses whose adresse/husnummer lifecycle disagree")

    for t in _HIST_TABLES:
        if not await has_table(reader, f"{staging}.{t}"):
            continue
        horizon = await reader.fetchval(f'SELECT min(registreringfra) FROM "{staging}".{t}')
        print(f"[i] {t} history horizon: {horizon or 'none'}")

    return lifecycle
