"""A ClickHouse table on a remote engine is listed as one whose rows live elsewhere (SPEC 2.2.20)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import ClickhouseAdapter
from dbprint.adapters.clickhouse import introspect
from dbprint.config.project import ConnectionConfig
from dbprint.engine import Engine
from tests._engine_run import conformance_errors
from tests.adapters._clickhouse import Recorder


_REMOTE = {
    "narrow": "URL('http://127.0.0.1:1/x.csv', CSV)",
    "wide": "S3('http://127.0.0.1:1/bucket/x.csv', 'key', 'secret', CSV)",
    "garden": "MySQL('127.0.0.1:1', 'db', 't', 'u', 'pw')",
    "cultivar": "PostgreSQL('127.0.0.1:1', 'db', 't', 'u', 'pw')",
}
_STORED = {
    "sample": "MergeTree ORDER BY id",
    "batch": "Memory",
    "fieldwork": "Buffer(seedbank, sample, 1, 10, 100, 10000, 1000000, 10000000, 100000000)",
    "herbarium_sheet": "Merge(seedbank, '^sample$')",
}


def test_every_documented_remote_engine_is_marked_by_name() -> None:
    assert {
        "URL",
        "S3",
        "MySQL",
        "PostgreSQL",
        "IcebergS3",
        "Executable",
    } <= introspect.REMOTE_ENGINES
    assert {"DeltaLakeLocal", "Hive", "ExternalDistributed", "MongoDB"} <= introspect.REMOTE_ENGINES
    assert not {"Distributed", "Merge", "Dictionary", "Buffer", "Memory", "MergeTree"} & (
        introspect.REMOTE_ENGINES
    )


def test_remote_tables_are_printed_from_the_catalog_and_none_fails(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    recorder = _seeded(clickhouse_native_connection)
    _generate(recorder, tmp_path)
    manifest = yaml.safe_load((tmp_path / "primary/manifest.yaml").read_text())

    for name in _REMOTE:
        statistics = _statistics(tmp_path, name)

        assert statistics["catalog_only"] is True, name
        assert statistics["external"] is True, name
        assert (tmp_path / "primary/seedbank" / name / "ddl.sql").is_file()

    for name in _STORED:
        assert "external" not in _statistics(tmp_path, name), name

    assert not manifest.get("failed_tables")
    assert [s for s in recorder.statements if _reads_remote(s)] == []
    assert conformance_errors(tmp_path / "primary") == []


def _seeded(cursor: Any) -> Recorder:
    for name, engine in (*_STORED.items(), *_REMOTE.items()):
        cursor.execute(f"CREATE TABLE seedbank.{name} (id UInt64) ENGINE = {engine}")

    return Recorder(cursor)


def _generate(cursor: Any, tmp_path: Path) -> None:
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    conn = ConnectionConfig(
        name="primary",
        adapter="clickhouse",
        output=tmp_path,
        include=("seedbank.*",),
    )

    try:
        Engine(adapter, conn, tmp_path).generate()
    finally:
        adapter.close()


def _statistics(tmp_path: Path, name: str) -> dict[str, Any]:
    return yaml.safe_load((tmp_path / "primary/seedbank" / name / "statistics.yaml").read_text())


def _reads_remote(statement: str) -> bool:
    lowered = statement.lower()

    return "system." not in lowered and any(re.search(rf"\b{name}\b", lowered) for name in _REMOTE)
