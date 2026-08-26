"""The engine's copy-the-draw seam: which scope reaches the adapter, and which the file.

`tests/adapters/test_sample_coherence.py` pins that a real server executes the copy; these
pin the decisions around it - when a copy is asked for, that the copied relation never
reaches the artifact, and that a refused write degrades rather than fails the table.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    MockAdapter,
    MockTable,
    StatisticsConfig,
    TableScope,
    ValueCount,
)
from dbprint.adapters.base import BaseStats, ColumnProgress, TableCounts
from dbprint.config import ConnectionConfig, RuleConfig
from dbprint.engine import Engine


COPY = "dbprint_sample_test"


class _RecordingAdapter(MockAdapter):
    """A mock that copies the draw as a real adapter would, and remembers who saw what.

    `refuse` models a missing privilege: the write raises and later calls keep the first scope.
    """

    def __init__(self, fixture: dict[str, MockTable], *, refuse: bool = False) -> None:
        super().__init__(fixture)
        self._refuse = refuse
        self.calls: list[tuple[str, TableScope | None]] = []
        self.released: list[str | None] = []

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        self.calls.append(("materialize", scope))

        if self._refuse:
            raise PermissionError("CREATE TEMPORARY TABLE denied for user 'reader'")

        return replace(scope, materialized=COPY)

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        del fqn

        self.released.append(scope.materialized)

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, dict[str, BaseStats]]:
        self.calls.append(("base", scope))

        return super().compute_base_statistics(fqn, columns, config, scope)

    def compute_column_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        fk_source_columns: frozenset[str],
        *,
        suppress_values: frozenset[str] = frozenset(),
        on_column: ColumnProgress | None = None,
        scope: TableScope | None = None,
    ) -> dict[str, ColumnStats]:
        self.calls.append(("columns", scope))

        return super().compute_column_statistics(
            fqn,
            columns,
            config,
            counts,
            base,
            fk_source_columns,
            suppress_values=suppress_values,
            on_column=on_column,
            scope=scope,
        )

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
    ) -> list[Any]:
        self.calls.append(("sample_values", scope))

        return super().sample_values(fqn, column, n, scope)

    def scopes_seen_by_statistics(self) -> list[TableScope | None]:
        """Every scope the three statistics entry points were handed, in call order."""

        return [scope for phase, scope in self.calls if phase != "materialize"]


class TestASampledTableIsCopied:
    def test_every_statistics_call_receives_the_copy(self, tmp_path: Path) -> None:
        """One draw for the whole table means one scope for every statement over it."""

        adapter = _run(tmp_path, RuleConfig(sample=0.25))
        seen = adapter.scopes_seen_by_statistics()

        assert seen, "no statistics call was made; the assertion would be vacuous"
        assert {"base", "sample_values", "columns"} <= {phase for phase, _ in adapter.calls}
        assert all(scope is not None and scope.materialized == COPY for scope in seen)

    def test_the_copy_is_released_once_the_table_is_done(self, tmp_path: Path) -> None:
        adapter = _run(tmp_path, RuleConfig(sample=0.25))

        assert adapter.released == [COPY]

    def test_the_artifact_names_the_fraction_and_never_the_copy(self, tmp_path: Path) -> None:
        """SPEC 2.2.8 forbids recording how the sample was drawn, and the copy's name is
        nothing else: unresolvable to a reader, and diff churn on every run.
        """

        _run(tmp_path, RuleConfig(sample=0.25))
        text = (tmp_path / "w" / "seedbank" / "vault" / "statistics.yaml").read_text()

        assert yaml.safe_load(text)["scope"] == {"rows_scanned": 250, "sample": 0.25}
        assert COPY not in text
        assert "materialized" not in text


class TestTheCopyIsNotAlwaysAskedFor:
    @pytest.mark.parametrize(
        "rule",
        [None, RuleConfig(filter="bucket = 0")],
        ids=["full_scan", "filtered"],
    )
    def test_only_a_drawn_fraction_is_worth_copying(
        self,
        tmp_path: Path,
        rule: RuleConfig | None,
    ) -> None:
        """A full scan has nothing to copy, and a predicate reselects the same rows."""

        adapter = _run(tmp_path, rule)

        assert [phase for phase, _ in adapter.calls if phase == "materialize"] == []
        assert adapter.released == []

    def test_the_switch_off_declines_the_write(self, tmp_path: Path) -> None:
        """The setting an organisation that forbids the tool writing anything chooses."""

        adapter = _run(tmp_path, RuleConfig(sample=0.25), materialize_sample=False)

        assert [phase for phase, _ in adapter.calls if phase == "materialize"] == []
        assert all(
            scope is not None and scope.materialized is None
            for scope in adapter.scopes_seen_by_statistics()
        )


class TestARefusedWriteDegrades:
    def test_the_table_is_still_profiled_from_the_unmaterialized_scope(
        self,
        tmp_path: Path,
    ) -> None:
        adapter = _run(tmp_path, RuleConfig(sample=0.25), refuse=True)
        seen = adapter.scopes_seen_by_statistics()

        assert seen, "the run gave up on the table instead of falling back"
        assert all(scope is not None and scope.materialized is None for scope in seen)
        assert (tmp_path / "w" / "seedbank" / "vault" / "statistics.yaml").is_file()

    def test_nothing_is_released_that_was_never_created(self, tmp_path: Path) -> None:
        adapter = _run(tmp_path, RuleConfig(sample=0.25), refuse=True)

        assert adapter.released == []

    def test_the_refusal_is_warned_about_rather_than_passed_over(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The numbers silently stop being coherent, so silence is the wrong answer."""

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            _run(tmp_path, RuleConfig(sample=0.25), refuse=True)

        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        about_the_copy = [m for m in warnings if "seedbank.vault" in m and "materialize" in m]

        assert about_the_copy, f"no warning named the table whose copy was refused: {warnings}"
        assert "denied" in about_the_copy[0], "the warning drops the reason the write failed"


def _run(
    tmp_path: Path,
    rule: RuleConfig | None,
    *,
    refuse: bool = False,
    materialize_sample: bool = True,
) -> _RecordingAdapter:
    adapter = _RecordingAdapter(_fixture(), refuse=refuse)
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        rules=() if rule is None else (rule,),
        materialize_sample=materialize_sample,
        infer_relationships=False,
    )
    Engine(adapter, conn, tmp_path).generate()

    return adapter


def _fixture() -> dict[str, MockTable]:
    """A thousand-row table whose statistics were measured over a quarter of it."""

    return {
        "seedbank.vault": MockTable(
            type="table",
            namespace_path=("seedbank", "vault"),
            ddl="CREATE TABLE seedbank.vault (bucket integer);\n",
            columns=[
                ColumnMeta(
                    name="bucket",
                    sql_type="integer",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "bucket": ColumnStats(
                    sql_type="integer",
                    nullable=False,
                    null_count=0,
                    null_rate=0.0,
                    cardinality=10,
                    cardinality_ratio=0.04,
                    cardinality_method="exact",
                    values=tuple(ValueCount(value=str(i), count=25) for i in range(10)),
                    values_coverage=1.0,
                    distribution="uniform",
                ),
            },
            samples={"bucket": ["1", "2", "3"]},
            row_count=1000,
            rows_scanned=250,
        ),
    }
