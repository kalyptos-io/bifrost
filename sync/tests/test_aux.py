"""aux gazetteer ports: pure accrual first-seen semantics, plus the gen-schema table write
(db-gated; skipped without a dsn). mirrors the retired load._Aux / _acc_aux.
"""

from __future__ import annotations

import os
from uuid import uuid4

import asyncpg
import pytest
from bifrost.arms.normalize import normalize
from bifrost.db import schema_sql
from bifrost_sync.snapshot.aux import Aux, acc_aux, write_aux_tables

_DSN = os.environ.get("BIFROST_DATABASE_DSN")
_needs_db = pytest.mark.skipif(not _DSN, reason="BIFROST_DATABASE_DSN unset")


def test_acc_aux_city_and_inverted_maps():
    aux = Aux()
    acc_aux({"postcode": "4850", "city": "Stubbekøbing", "sub_locality": "Nykøbing"}, aux)
    acc_aux({"postcode": "8400", "city": "Ebeltoft"}, aux)
    acc_aux({"foo": "bar"}, aux)  # no postcode -> ignored

    assert aux.postcodes == {"4850", "8400"}
    assert aux.city_map[normalize("Stubbekøbing")] == {"4850"}
    assert aux.city_map[normalize("Ebeltoft")] == {"8400"}
    assert aux.subloc_map[normalize("Nykøbing")] == {"4850"}


@_needs_db
async def test_write_aux_tables_round_trips():
    conn = await asyncpg.connect(_DSN)
    schema = f"sync_test_{uuid4().hex}"
    try:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}", public')
        await conn.execute(schema_sql())

        aux = Aux()
        acc_aux({"postcode": "4850", "city": "Stubbekøbing", "sub_locality": "Nykøbing"}, aux)
        acc_aux({"postcode": "8400", "city": "Ebeltoft"}, aux)
        acc_aux({"postcode": "9999"}, aux)  # city-less: in the dim, not the display map
        await write_aux_tables(conn, schema, aux)

        dim = {r["postcode"] for r in await conn.fetch("SELECT postcode FROM aux_postcode_dim")}
        assert dim == {"4850", "8400", "9999"}  # city-less postcode still in the dimension
        city_rows = await conn.fetch("SELECT * FROM aux_city_map")
        city = {(r["folded_name"], r["postcode"]) for r in city_rows}
        assert city == {(normalize("Stubbekøbing"), "4850"), (normalize("Ebeltoft"), "8400")}
        subloc_rows = await conn.fetch("SELECT * FROM aux_subloc_map")
        assert {(r["folded_name"], r["postcode"]) for r in subloc_rows} == {
            (normalize("Nykøbing"), "4850")
        }
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await conn.close()
