"""What the engine writes for a column's `unmeasured` marker (SPEC 2.2.4).

The adapter reports what its read lost; the engine names only what the artifact owed and lacks.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from dbprint.adapters import (
    BaseStats,
    ColumnMeta,
    ColumnStats,
    Frequencies,
    MockAdapter,
    MockTable,
    Range,
    ValueCount,
)
from dbprint.adapters.base import PhaseA, TableCounts
from dbprint.config import ConnectionConfig
from dbprint.conformance.schema_validation import check_statistics
from dbprint.conformance.statistics import check
from dbprint.engine import Engine
from tests._engine_run import artifact, conformance_errors
from tests._prints import columns, mock_table


_LOST = ("distribution", "frequencies", "values")


class TestWhatReachesTheArtifact:
    def test_a_genuinely_lost_required_field_is_named(self, tmp_path: Path) -> None:
        column = _degraded(unmeasured=_LOST)

        assert _column(_generate(tmp_path, column))["unmeasured"] == sorted(_LOST)

    def test_a_name_the_column_also_emits_is_dropped(self, tmp_path: Path) -> None:
        """`cardinality_method` is computed before the failing statement and emitted regardless,
        so naming it would be `stats.unmeasured-names-emitted-field` on every degraded column.
        """

        column = _degraded(unmeasured=("cardinality_method", *_LOST))
        payload = _column(_generate(tmp_path, column))

        assert payload["unmeasured"] == sorted(_LOST)
        assert payload["cardinality_method"] == "exact"

    def test_a_name_the_classification_never_required_is_dropped(self, tmp_path: Path) -> None:
        """`mean` is forbidden outside `numeric`, so its absence is structural and needs no
        marker - naming it is `stats.unmeasured-names-unrequired-field`.
        """

        column = _degraded(unmeasured=("mean", *_LOST))

        assert _column(_generate(tmp_path, column))["unmeasured"] == sorted(_LOST)

    def test_a_column_that_lost_nothing_carries_no_key(self, tmp_path: Path) -> None:
        assert "unmeasured" not in _column(_generate(tmp_path, _measured()))

    def test_the_degraded_column_validates(self, tmp_path: Path) -> None:
        """Eight required-field errors become none; both misuses are filtered already."""

        payload = _generate(tmp_path, _degraded(unmeasured=("cardinality_method", *_LOST)))
        codes = {i.code for i in check(payload, "statistics.yaml", "seedbank.accession")}

        assert codes == set()
        # Both layers run over the same file in one `dbprint check`, so the schema has to
        # relax the classification's own required list wherever the marker is present.
        assert check_statistics(payload, "statistics.yaml") == []


class TestAColumnPhaseACouldNotMeasure:
    """A column phase A could only null-count is published from the catalog and claimed by nothing."""

    def test_it_is_classified_from_its_type_and_names_what_it_lost(self, tmp_path: Path) -> None:
        payload, _ = _generate_degraded(tmp_path)
        grade = payload["columns"]["grade"]

        assert grade["classification"] == "numeric"
        assert grade["null_count"] == 12
        assert grade["null_rate"] == 0.03  # noqa: RUF069 - the expected value is an exact literal
        assert "cardinality" not in grade
        assert grade["unmeasured"] == [
            "cardinality",
            "cardinality_method",
            "cardinality_ratio",
            "distribution",
            "frequencies",
            "mean",
            "negative_count",
            "percentiles",
            "quantized_count",
            "range",
            "sum",
            "values",
            "zero_count",
        ]

    def test_the_table_is_written_in_catalog_order(self, tmp_path: Path) -> None:
        payload, _ = _generate_degraded(tmp_path)

        assert list(payload["columns"]) == ["logged_at", "grade"]

    def test_no_later_pass_reads_it(self, tmp_path: Path) -> None:
        _, adapter = _generate_degraded(tmp_path)

        assert adapter.phase_b_columns == [["logged_at"]]
        assert adapter.null_pattern_calls == 0

    def test_no_sketch_is_taken_of_it(self, tmp_path: Path) -> None:
        payload, adapter = _generate_degraded(tmp_path, sketch_all_columns=True)

        assert "sketch" not in payload["columns"]["grade"]
        assert "grade" not in adapter.sketched

    def test_the_cross_column_blocks_are_named_unmeasured(self, tmp_path: Path) -> None:
        payload, _ = _generate_degraded(tmp_path)

        assert {"null_patterns", "dependencies"} <= set(payload["unmeasured"])
        assert "null_patterns" not in payload

    def test_the_engine_warns_once_for_the_table(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            _generate_degraded(tmp_path)

        warned = [r.getMessage() for r in caplog.records if "could not measure" in r.getMessage()]

        assert len(warned) == 1
        assert "grade" in warned[0]
        assert "forced" in warned[0]

    def test_the_degraded_column_and_its_table_conform(self, tmp_path: Path) -> None:
        """`logged_at` is the shared control fixture, which states no coherent distribution."""

        _generate_degraded(tmp_path)
        errors = [
            i
            for i in conformance_errors(tmp_path / "w")
            if not i.path.endswith("columns.logged_at")
        ]

        assert errors == []


class _RecordingMock(MockAdapter):
    def __init__(self, fixture: dict[str, MockTable], responses: dict[str, list[Any]]) -> None:
        super().__init__(fixture, responses=responses)
        self.phase_b_columns: list[list[str]] = []
        self.null_pattern_calls = 0
        self.sketched: list[str] = []

    def compute_column_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self.phase_b_columns.append([c.name for c in columns])

        return super().compute_column_statistics(fqn, columns, *args, **kwargs)

    def compute_key_sketch(self, fqn: str, column: str, *args: Any, **kwargs: Any) -> Any:
        self.sketched.append(column)

        return super().compute_key_sketch(fqn, column, *args, **kwargs)

    def compute_null_patterns(self, *args: Any, **kwargs: Any) -> Any:
        self.null_pattern_calls += 1

        return super().compute_null_patterns(*args, **kwargs)


def _generate_degraded(
    tmp_path: Path,
    *,
    sketch_all_columns: bool = False,
) -> tuple[dict[str, Any], _RecordingMock]:
    fixture = _fixture(_measured())
    table = fixture["seedbank.accession"]
    grade = ColumnMeta(name="grade", sql_type="integer", nullable=True, default=None, ordinal=2)
    fixture["seedbank.accession"] = replace(table, columns=[*table.columns, grade])
    phase_a = PhaseA(
        stats={"logged_at": BaseStats(null_count=0, cardinality=400, cardinality_method="exact")},
        unmeasured={"grade": 12},
        failures=(RuntimeError("forced"),),
    )
    adapter = _RecordingMock(
        fixture,
        {"compute_base_statistics": [(TableCounts(row_count=400, rows_scanned=400), phase_a)]},
    )
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
        sketch_all_columns=sketch_all_columns,
    )
    Engine(adapter, conn, tmp_path).generate()
    payload = artifact(tmp_path / "w", "seedbank.accession")

    return payload, adapter


def _generate(tmp_path: Path, column: ColumnStats) -> dict[str, Any]:
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
    )
    Engine(MockAdapter(_fixture(column)), conn, tmp_path).generate()

    return artifact(tmp_path / "w", "seedbank.accession")


def _column(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["columns"]["logged_at"]


def _measured() -> ColumnStats:
    """A temporal column whose every read answered - the control the degrades vary from."""

    return ColumnStats(
        sql_type="timestamp",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=400,
        cardinality_ratio=1.0,
        cardinality_method="exact",
        values=(ValueCount(value="2025-12-31T00:00:00Z", count=400),),
        distribution="uniform",
        frequencies=Frequencies(top=400, bottom=400, listed=1, total=400),
        range=Range(min="2025-11-01T00:00:00Z", max="2025-12-31T00:00:00Z", span_days=60),
        percentiles={"p50": "2025-12-01T00:00:00Z"},
        quantized_count=60,
    )


def _degraded(*, unmeasured: tuple[str, ...]) -> ColumnStats:
    """The same column with the top-N outputs absent and `unmeasured` naming them."""

    return replace(
        _measured(),
        values=None,
        distribution=None,
        frequencies=None,
        unmeasured=unmeasured,
    )


def _fixture(column: ColumnStats) -> dict[str, MockTable]:
    return {
        "seedbank.accession": mock_table(
            "seedbank.accession",
            columns(("logged_at", "timestamp")),
            {"logged_at": column},
            ddl="CREATE TABLE seedbank.accession (logged_at timestamp);\n",
            row_count=400,
        ),
    }
