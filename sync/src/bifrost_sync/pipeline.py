"""dlt staging factory: per-entity merge resources into the persistent "datafordeler" dataset.

one resource per EntitySpec (merge on pk_out, explicit column hints, frozen schema contract so
upstream drift fails loudly). the fold's tombstones ride a `_deleted` hard_delete column, so a merge
applies upserts and deletes in one pass. each resource stamps its new per-entity cursor into dlt
resource state inside the generator, so the cursor commits atomically with the load. baseline and
delta share the resource shape; baseline adds refresh="drop_resources" to reset table + cursor.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable, Iterator
from typing import Any, NamedTuple

import dlt
from dlt.common.pipeline import TRefreshMode
from dlt.common.schema.typing import TColumnSchema, TDataType, TSchemaContractDict

from .config import Config
from .registers import DAGI, DAR, DS, EBR, MAT, EntitySpec, Kind, contract_hash

_SOURCE_NAME = {DAR: "dar", DAGI: "dagi", MAT: "mat", DS: "ds", EBR: "ebr"}
_CONTRACT: TSchemaContractDict = {"tables": "evolve", "columns": "freeze", "data_type": "freeze"}
# geojson/point-text/timestamp arrive pre-serialized from extract - pin text so dlt never re-types
_KIND_TYPE: dict[Kind, TDataType] = {
    Kind.TEXT: "text",
    Kind.DOUBLE: "double",
    Kind.GEOJSON: "text",
    Kind.POINT_TEXT: "text",
    Kind.TIMESTAMP: "text",
}


class Load(NamedTuple):
    spec: EntitySpec
    rows: Iterable[dict]
    cursor: int  # per-entity generationNumber to commit with this load


def hints(spec: EntitySpec) -> dict[str, TColumnSchema]:
    """dlt column hints from the spec's Kinds; the tombstone signal is a bool hard_delete column."""
    cols: dict[str, TColumnSchema] = {}
    for c in spec.columns:
        if c.kind is Kind.POINT_XY:
            kx, ky = c.name
            cols[kx] = {"data_type": "double"}
            cols[ky] = {"data_type": "double"}
        else:
            assert not isinstance(c.name, tuple)  # only POINT_XY carries a 2-tuple name
            cols[c.name] = {"data_type": _KIND_TYPE[c.kind]}
    cols["_deleted"] = {"data_type": "bool", "hard_delete": True}
    return cols


def entity_resource(spec: EntitySpec, rows: Iterable[dict], new_cursor: int):
    @dlt.resource(
        name=spec.table,
        primary_key=spec.merge_key,  # composite (pk + version_key) on a hist spec, else pk alone
        write_disposition="merge",
        columns=hints(spec),
        schema_contract=_CONTRACT,
    )
    def resource() -> Iterator[dict]:
        state = dlt.current.resource_state()  # cursor + contract commit atomically with the load
        state["gen"] = new_cursor
        state["contract"] = contract_hash(spec)
        n = 0
        for row in rows:
            n += 1
            yield row
        # a zero-row load creates no table; the emptiness check must not read that as a lost load
        state["rows"] = n

    return resource()


def _configure_env() -> None:
    os.environ.setdefault("RUNTIME__LOG_LEVEL", "WARNING")
    os.environ.setdefault("NORMALIZE__WORKERS", str(min(os.cpu_count() or 1, 4)))
    os.environ.setdefault("DATA_WRITER__FILE_MAX_ITEMS", "200000")
    os.environ["LOAD__WORKERS"] = "1"
    os.environ.setdefault("LOAD__TRUNCATE_STAGING_DATASET", "true")


def make_pipeline(
    cfg: Config, *, dataset: str = "datafordeler", pipeline_name: str = "datafordeler"
) -> dlt.Pipeline:
    _configure_env()
    return dlt.pipeline(
        pipeline_name=pipeline_name,
        dataset_name=dataset,
        destination=dlt.destinations.postgres(credentials=cfg.dsn),
        pipelines_dir=str(cfg.work_dir / ".dlt"),
    )


def _by_register(loads: Iterable[Load]) -> dict[str, list[Load]]:
    groups: dict[str, list[Load]] = defaultdict(list)
    for load in loads:
        groups[load.spec.register].append(load)
    return groups


def _source(register: str, loads: list[Load]):
    @dlt.source(name=_SOURCE_NAME[register])
    def src() -> list:
        return [entity_resource(load.spec, load.rows, load.cursor) for load in loads]

    return src()


def run_loads(
    pipeline: dlt.Pipeline, loads: Iterable[Load], *, refresh: TRefreshMode | None = None
) -> list:
    # refresh="drop_resources" (baseline): drop tables + state so a full total repopulates clean
    infos = []
    for register, group in _by_register(loads).items():
        infos.append(
            pipeline.run(
                _source(register, group),
                loader_file_format="csv",
                # dlt declares refresh as TRefreshMode but defaults it to None
                refresh=refresh,  # ty: ignore[invalid-argument-type]
            )
        )
    return infos


def _state_field(pipeline: dlt.Pipeline, key: str) -> dict[str, Any]:
    """{table -> committed `key`} across all register sources in pipeline state."""
    out: dict[str, Any] = {}
    for src_state in pipeline.state.get("sources", {}).values():
        for table, rstate in src_state.get("resources", {}).items():
            if key in rstate:
                out[table] = rstate[key]
    return out


def cursors(pipeline: dlt.Pipeline) -> dict[str, int]:
    return _state_field(pipeline, "gen")


def contracts(pipeline: dlt.Pipeline) -> dict[str, str]:
    return _state_field(pipeline, "contract")


def staged_rows(pipeline: dlt.Pipeline) -> dict[str, int]:
    """{table -> rows the last committed load staged}."""
    return _state_field(pipeline, "rows")


def drain(pipeline: dlt.Pipeline) -> None:
    """recover: pull destination state, then finish any load package a prior crash left pending."""
    pipeline.sync_destination()
    pipeline.normalize()
    pipeline.load()
