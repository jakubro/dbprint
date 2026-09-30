"""A statement the connection's own limit cancelled is reported as a timeout, once, everywhere."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from dbprint.adapters import ColumnMeta, ColumnStats, CommentsMeta, MockAdapter, MockTable
from dbprint.adapters.base import PhaseA, TableCounts, TableScope, run_phase_b
from dbprint.adapters.errors import QueryFailed
from dbprint.config import ConnectionConfig, StatisticsConfig
from dbprint.conformance import validate_print
from dbprint.engine import Engine, GenerateResult


class _TimesOut(MockAdapter):
    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, PhaseA]:
        raise QueryFailed(RuntimeError("canceling statement"), "SELECT 1", timed_out=True)


class TestATimedOutCoreStatistic:
    def test_the_table_fails_saying_it_timed_out(self, tmp_path: Path) -> None:
        result = _generate(tmp_path, statement_timeout=600)
        failed = [t for t in result.tables if t.status == "failed"]

        assert [t.error for t in failed] == ["timed out after 10m", "timed out after 10m"]

    def test_without_a_configured_limit_the_driver_message_stands(self, tmp_path: Path) -> None:
        """An account-side limit is not reported as dbprint's own."""

        result = _generate(tmp_path, statement_timeout=None)

        assert {t.error for t in result.tables} == {"RuntimeError: canceling statement"}


class _ColumnTimesOut(MockAdapter):
    def compute_column_statistics(self, fqn: str, columns: list[ColumnMeta], *args, **kwargs):
        def measure(column: ColumnMeta) -> ColumnStats:
            raise QueryFailed(RuntimeError("canceling statement"), "SELECT 1", timed_out=True)

        return run_phase_b(columns, measure)


class TestATimedOutColumnStatistic:
    def test_the_column_degrades_saying_it_timed_out_and_the_table_profiles(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        conn = ConnectionConfig(
            name="w",
            adapter="postgres",
            output=tmp_path,
            infer_relationships=False,
            statement_timeout=600,
        )

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            result = Engine(_ColumnTimesOut({"s.a": _table("a")}), conn, tmp_path).generate()

        column = yaml.safe_load((tmp_path / "w" / "s" / "a" / "statistics.yaml").read_text())
        warned = [r for r in caplog.records if "s.a" in r.getMessage()]

        assert [t.status for t in result.tables] == ["ok"]
        assert column["columns"]["id"]["unmeasured"]
        assert [r.levelno for r in warned] == [logging.WARNING]
        assert "timed out after 10m" in warned[0].getMessage()
        assert [i.code for i in validate_print(tmp_path / "w") if i.severity == "error"] == []


def _generate(tmp_path: Path, statement_timeout: int | None) -> GenerateResult:
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
        statement_timeout=statement_timeout,
    )

    return Engine(_TimesOut({"s.a": _table("a"), "s.b": _table("b")}), conn, tmp_path).generate()


def _table(name: str) -> MockTable:
    return MockTable(
        type="table",
        namespace_path=("s", name),
        ddl=f"CREATE TABLE s.{name} (id int);\n",
        columns=[ColumnMeta(name="id", sql_type="int", nullable=False, default=None, ordinal=1)],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": ColumnStats(
                sql_type="int",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=1,
                cardinality_ratio=1.0,
                cardinality_method="exact",
            ),
        },
        samples={},
        row_count=1,
    )
