"""A Redshift Spectrum table is listed as a table whose rows live elsewhere (SPEC 2.2.20)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import RedshiftAdapter
from dbprint.config.project import RuleConfig
from dbprint.conformance import validate_print
from dbprint.engine import Engine
from tests.adapters.conftest import ExternalTableSeam, RedshiftDialectShim
from tests.adapters.test_redshift import redshift_scratch_db  # noqa: F401
from tests.engine.test_orchestrator import _conn_config


REMOTE = "seedbank.seedbank.remote_reading"
_DDL = (
    "CREATE EXTERNAL TABLE seedbank.remote_reading (\n"
    "    id int,\n    vault_id int\n)\nPARTITIONED BY (read_on date)\n"
    "STORED AS PARQUET\nLOCATION 's3://bucket/readings/';\n"
)
_SEAMS = {
    "seedbank.remote_reading": ExternalTableSeam(
        columns=(
            ("id", "int", "false", 0),
            ("vault_id", "int", "true", 0),
            ("read_on", "date", " ", 1),
        ),
        ddl=_DDL,
    ),
    "seedbank.remote_reading_v": ExternalTableSeam(columns=(), ddl="", tabletype="VIEW"),
}


class _Recorder(RedshiftDialectShim):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = None) -> RedshiftDialectShim:
        self.statements.append(" ".join(sql.split()))

        return super().execute(sql, params)


def _seeded(conn: Any) -> _Recorder:
    conn.execute("CREATE SCHEMA seedbank")
    conn.execute("CREATE TABLE seedbank.vault (id int PRIMARY KEY)")
    conn.execute("CREATE TABLE seedbank.remote_reading (id int, vault_id int, read_on date)")
    conn.execute(
        "INSERT INTO seedbank.remote_reading "
        "SELECT g, g % 5, DATE '2026-01-01' + g FROM generate_series(1, 40) g",
    )

    return _Recorder(conn, external_tables=_SEAMS)


def _adapter(shim: RedshiftDialectShim) -> RedshiftAdapter:
    return RedshiftAdapter(
        {"host": "redshift", "database": "seedbank", "user": "test", "password": "test"},
        cursor_factory=lambda _params: shim,
    )


def _listed(shim: RedshiftDialectShim, exclude: list[str]) -> dict[str, Any]:
    adapter = _adapter(shim)
    adapter.connect()

    try:
        return {t.fqn: t for t in adapter.list_tables(include=["*"], exclude=exclude)}
    finally:
        adapter.close()


def test_an_external_table_is_listed_beside_local_ones_and_an_external_view_is_not(
    redshift_scratch_db: Any,  # noqa: F811
) -> None:
    listed = _listed(_seeded(redshift_scratch_db), [])

    assert listed[REMOTE].type == "table"
    assert listed[REMOTE].external is True
    assert listed["seedbank.seedbank.vault"].external is False
    assert "seedbank.seedbank.remote_reading_v" not in listed


def test_an_excluded_external_table_is_not_listed(redshift_scratch_db: Any) -> None:  # noqa: F811
    assert REMOTE not in _listed(_seeded(redshift_scratch_db), ["seedbank.seedbank.*"])


def test_columns_ddl_and_layout_come_from_the_external_catalog(
    redshift_scratch_db: Any,  # noqa: F811
) -> None:
    adapter = _adapter(_seeded(redshift_scratch_db))
    adapter.connect()

    try:
        adapter.list_tables(include=["*"], exclude=[])
        columns = adapter.introspect_columns(REMOTE)
        ddl = adapter.extract_ddl(REMOTE)
        layout = adapter.introspect_physical_layout(REMOTE)
    finally:
        adapter.close()

    assert [(c.name, c.sql_type, c.nullable, c.ordinal) for c in columns] == [
        ("id", "int", False, 1),
        ("vault_id", "int", True, 2),
        ("read_on", "date", True, 3),
    ]
    assert ddl == _DDL
    assert layout is not None
    assert layout.mechanism == "partition"
    assert [k.column for k in layout.keys] == ["read_on"]


def test_a_catalog_only_print_reads_none_of_its_rows(
    redshift_scratch_db: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    shim = _seeded(redshift_scratch_db)
    Engine(_adapter(shim), _conn(tmp_path), tmp_path).generate()
    statistics = _statistics(tmp_path)

    assert statistics["catalog_only"] is True
    assert statistics["external"] is True
    assert statistics["physical_layout"]["mechanism"] == "partition"
    assert "row_count" not in statistics
    assert (tmp_path / "primary/seedbank/seedbank/remote_reading/ddl.sql").read_text() == _DDL
    assert [s for s in shim.statements if "remote_reading" in s and "SHOW EXTERNAL" not in s] == []
    assert [i for i in validate_print(tmp_path / "primary") if i.severity == "error"] == []


def test_an_opted_in_external_table_is_profiled(
    redshift_scratch_db: Any,  # noqa: F811
    tmp_path: Path,
) -> None:
    conn = _conn(tmp_path, RuleConfig(include=(REMOTE,), read_rows=True))
    Engine(_adapter(_seeded(redshift_scratch_db)), conn, tmp_path).generate()
    statistics = _statistics(tmp_path)

    assert statistics["row_count"] == 40
    assert statistics["external"] is True
    assert "catalog_only" not in statistics


def _conn(tmp_path: Path, *rules: RuleConfig) -> Any:
    return replace(_conn_config(tmp_path), adapter="redshift", rules=rules)


def _statistics(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "primary/seedbank/seedbank/remote_reading/statistics.yaml"

    return yaml.safe_load(path.read_text())
