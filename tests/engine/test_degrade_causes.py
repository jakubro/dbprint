"""A statistic block that degrades to `unmeasured` says why, and a timeout is named as one."""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any, cast

import duckdb
import pytest
import yaml

import dbprint.adapters as adapters_package
from dbprint.adapters import DuckdbAdapter
from dbprint.adapters import base as base_module
from dbprint.adapters.databricks import introspect as databricks_introspect
from dbprint.adapters.duckdb import stats as duckdb_stats
from dbprint.adapters.errors import QueryFailed
from dbprint.adapters.identifiers import Identity
from dbprint.adapters.redshift import ddl as redshift_ddl
from dbprint.adapters.redshift.connection import DIALECT as REDSHIFT
from dbprint.config import ConnectionConfig
from dbprint.config.project import RedactRule
from dbprint.engine import Engine
from dbprint.spec.rounding import UnrepresentableValue
from tests._engine_run import conformance_errors


_PACKAGE = Path(adapters_package.__file__).parent


@pytest.mark.parametrize(
    ("timed_out", "warning"),
    [
        (True, "table 'garden.main.sown': column 'sown_at' temporal statistics timed out after 1s"),
        (
            False,
            (
                "table 'garden.main.sown': column 'sown_at' temporal statistics failed: "
                "RuntimeError: division by zero"
            ),
        ),
    ],
)
def test_a_degraded_temporal_block_is_named_with_its_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    timed_out: bool,
    warning: str,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(
        "CREATE TABLE sown AS "
        "SELECT TIMESTAMP '2024-01-01' + INTERVAL (i) DAY AS sown_at FROM range(500) r(i)",
    )
    con.close()

    def failing(*_args: Any, **_kwargs: Any) -> Any:
        raise QueryFailed(RuntimeError("division by zero"), "SELECT", timed_out=timed_out)

    monkeypatch.setattr(duckdb_stats, "_fetch_temporal_block", failing)
    conn = ConnectionConfig(
        name="g",
        adapter="duckdb",
        output=tmp_path / "prints",
        infer_relationships=False,
        statement_timeout=1,
    )

    with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
        Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate()

    statistics = yaml.safe_load(next((tmp_path / "prints").rglob("statistics.yaml")).read_text())
    messages = [r.getMessage() for r in caplog.records]
    assert "range" in statistics["columns"]["sown_at"]["unmeasured"]
    assert [m for m in messages if "column 'sown_at'" in m] == [warning]


def test_every_degrading_block_hands_its_cause_to_the_engine() -> None:
    bare = [
        f"{path.relative_to(_PACKAGE)}:{handler.lineno}"
        for path in sorted(_PACKAGE.glob("*/stats.py"))
        for handler in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(handler, ast.ExceptHandler)
        and _degrades(handler)
        and not _names_cause(handler)
    ]

    assert bare == []


def test_no_degrading_block_absorbs_an_unrepresentable_value() -> None:
    absorbing = [
        f"{path.relative_to(_PACKAGE)}:{node.lineno}"
        for path in sorted(_PACKAGE.glob("*/stats.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Try)
        and any(_degrades(handler) for handler in node.handlers)
        and not any(_names(handler, "UnrepresentableValue") for handler in node.handlers)
    ]

    assert absorbing == []


def test_an_unrepresentable_temporal_value_fails_its_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(
        "CREATE TABLE sown AS "
        "SELECT TIMESTAMP '2024-01-01' + INTERVAL (i) DAY AS sown_at FROM range(500) r(i)",
    )
    con.close()

    def unspellable(*_args: Any, **_kwargs: Any) -> Any:
        raise UnrepresentableValue(object(), field="range.min")

    monkeypatch.setattr(duckdb_stats, "_fetch_temporal_block", unspellable)
    conn = ConnectionConfig(name="g", adapter="duckdb", output=tmp_path / "prints")

    result = Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate()

    assert [t.status for t in result.tables] == ["failed"]
    assert "range.min is object" in (result.tables[0].error or "")


def test_an_unmeasured_timeline_anchor_costs_the_timeline_not_the_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(
        "CREATE TABLE sown AS SELECT i AS id, "
        "TIMESTAMP '2024-01-01' + INTERVAL (i) DAY AS sown_at FROM range(500) r(i)",
    )
    con.close()
    real = base_module.assemble_column_stats

    def failing(*args: Any, **kwargs: Any) -> Any:
        if any(getattr(a, "name", None) == "sown_at" for a in args):
            raise RuntimeError("no operator")

        return real(*args, **kwargs)

    monkeypatch.setattr(base_module, "assemble_column_stats", failing)
    conn = ConnectionConfig(name="g", adapter="duckdb", output=tmp_path / "prints")

    result = Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate()

    statistics = yaml.safe_load(next((tmp_path / "prints").rglob("statistics.yaml")).read_text())
    assert [t.status for t in result.tables] == ["ok"]
    assert "timeline" not in statistics
    assert statistics["columns"]["sown_at"]["unmeasured"]


def test_a_column_phase_b_could_not_measure_keeps_what_phase_a_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(
        "CREATE TABLE plot AS SELECT i AS id, CASE WHEN i % 5 = 0 THEN '' "
        "ELSE 'bed ' || (i % 40) END AS label FROM range(500) r(i)",
    )
    con.close()
    real = base_module.assemble_column_stats

    def failing(*args: Any, **kwargs: Any) -> Any:
        if any(getattr(a, "name", None) == "label" for a in args):
            raise RuntimeError("no operator")

        return real(*args, **kwargs)

    monkeypatch.setattr(base_module, "assemble_column_stats", failing)
    conn = ConnectionConfig(name="g", adapter="duckdb", output=tmp_path / "prints")

    Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate()

    root = tmp_path / "prints" / "g"
    label = yaml.safe_load(next(root.rglob("statistics.yaml")).read_text())["columns"]["label"]
    assert label["length"] == {"min": 0, "max": 6, "avg": 4.592, "p95": 6.0}
    assert "length" not in label["unmeasured"]
    assert [i.code for i in conformance_errors(root)] == []


def test_a_degraded_column_never_names_a_field_it_never_owed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "garden.duckdb"
    con = duckdb.connect(str(database))
    con.execute(
        "CREATE TABLE trial AS SELECT i AS id, i * 1.5 AS viability_pct, "
        "'seed lot ' || i || ' sprouted slowly after the second watering' AS field_notes "
        "FROM range(400) r(i)",
    )
    con.close()
    real = base_module.assemble_column_stats

    def failing(*args: Any, **kwargs: Any) -> Any:
        if any(getattr(a, "name", None) in {"viability_pct", "field_notes"} for a in args):
            raise RuntimeError("no operator")

        return real(*args, **kwargs)

    monkeypatch.setattr(base_module, "assemble_column_stats", failing)
    conn = ConnectionConfig(
        name="g",
        adapter="duckdb",
        output=tmp_path / "prints",
        redact=(RedactRule(columns=("*.viability_pct",), with_="drop"),),
    )

    Engine(DuckdbAdapter({"database": str(database)}), conn, tmp_path).generate()

    root = tmp_path / "prints" / "g"
    columns = yaml.safe_load(next(root.rglob("statistics.yaml")).read_text())["columns"]
    assert columns["field_notes"]["inferred"]["looks_like"] == "prose"
    assert not {"range", "percentiles"} & set(columns["viability_pct"].get("unmeasured", ()))
    assert not {"values", "distribution"} & set(columns["field_notes"].get("unmeasured", ()))
    assert [i.code for i in conformance_errors(root)] == []


def test_a_timed_out_show_table_is_not_retried_as_a_view() -> None:
    issued: list[str] = []

    def timing_out(cursor: Any, sql: str, params: Any = None) -> Any:
        issued.append(sql.split()[1])
        raise QueryFailed(RuntimeError("canceling statement"), sql, timed_out=True)

    identity = Identity.of(("dev", "public", "bed"), REDSHIFT)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(redshift_ddl, "exec_query", timing_out)

        with pytest.raises(QueryFailed):
            redshift_ddl.extract_ddl(cast(Any, object()), identity)

    assert issued == ["TABLE"]


def test_a_timed_out_describe_detail_is_not_read_as_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timing_out(cursor: Any, sql: str, params: Any = None) -> Any:
        raise QueryFailed(RuntimeError("canceling statement"), sql, timed_out=True)

    monkeypatch.setattr(databricks_introspect, "exec_query", timing_out)

    with pytest.raises(QueryFailed):
        databricks_introspect._describe_detail(cast(Any, object()), "`s`.`t`")


def _degrades(handler: ast.ExceptHandler) -> bool:
    return any(
        isinstance(node, ast.keyword) and node.arg == "unmeasured" for node in ast.walk(handler)
    )


def _names(handler: ast.ExceptHandler, name: str) -> bool:
    return isinstance(handler.type, ast.Name) and handler.type.id == name


def _names_cause(handler: ast.ExceptHandler) -> bool:
    return handler.name is not None and any(
        isinstance(node, ast.keyword) and node.arg == "unmeasured_cause"
        for node in ast.walk(handler)
    )
