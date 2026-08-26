"""A prose column pays for no enumeration (SPEC 2.2.3).

The saving is the grouped scan, not the bytes, so the engine classifies from Phase A and
tells Phase B which value lists not to build. Both the artifact and the request are
asserted: an adapter that ignored the request would still produce a conforming print.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import (
    BaseStats,
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    MockAdapter,
    MockTable,
    StatisticsConfig,
    TableCounts,
    TableScope,
    ValueCount,
)
from dbprint.adapters.base import ColumnProgress
from dbprint.conformance import validate_print
from dbprint.engine import Engine
from tests.engine.test_orchestrator import _conn_config


PROSE = [f"the quick brown fox number {i} jumped over a lazy dog today" for i in range(40)]
EMAILS = [f"user{i}@example.com" for i in range(40)]


class TestASuppressedColumnEmitsNoList:
    def test_a_prose_column_carries_no_value_fields(self, tmp_path: Path) -> None:
        field_notes = _profile(tmp_path)["field_notes"]

        assert field_notes["classification"] == "text"
        assert field_notes["inferred"]["looks_like"] == "prose"
        assert "values" not in field_notes
        assert "values_coverage" not in field_notes
        assert "distribution" not in field_notes

    def test_every_other_measurement_survives(self, tmp_path: Path) -> None:
        """The exemption removes the enumeration, never the statistics."""

        field_notes = _profile(tmp_path)["field_notes"]

        assert field_notes["cardinality"] == 100
        assert field_notes["cardinality_ratio"] == 0.5
        assert field_notes["null_count"] == 0
        assert field_notes["sql_type"] == "text"

    def test_an_ordinary_text_column_keeps_its_list(self, tmp_path: Path) -> None:
        """The control: only a prose verdict suppresses anything."""

        institution = _profile(tmp_path)["institution"]

        assert institution["classification"] == "text"
        assert institution["inferred"]["looks_like"] == "email"
        assert len(institution["values"]) == 20
        assert institution["values_coverage"] == 0.2
        assert institution["distribution"] == "long_tail"

    def test_a_categorical_column_reporting_prose_keeps_its_list(self, tmp_path: Path) -> None:
        """The exemption reaches `text` alone; categorical's matrix row requires the list."""

        status = _profile(tmp_path)["status"]

        assert status["classification"] == "categorical"
        assert status["inferred"]["looks_like"] == "prose"
        assert status["values"]
        assert status["values_coverage"] == 1.0
        assert status["distribution"]

    def test_the_print_conforms(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]

        assert errors == [], "\n".join(f"  {e.code} at {e.path}: {e.detail}" for e in errors)


class TestTheEngineAsksForTheSkip:
    """The artifact cannot distinguish a skipped scan from a discarded result."""

    def test_only_the_prose_columns_are_suppressed(self, tmp_path: Path) -> None:
        adapter = _RecordingAdapter(_fixture())
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert adapter.suppressed == {"field_notes", "phone"}

    def test_the_request_reaches_phase_b_before_it_runs(self, tmp_path: Path) -> None:
        """Phase A must not receive it - by then there is nothing left to skip."""

        adapter = _RecordingAdapter(_fixture())
        Engine(adapter, _conn_config(tmp_path), tmp_path).generate()

        assert adapter.calls == ["base", "columns"]


class TestTheConcreteCompositeStillAgrees:
    """`compute_statistics` runs the two halves, so every existing caller is unmoved."""

    def test_the_composite_matches_the_halves_run_in_order(self) -> None:
        """Literals `_fixture()` states directly, not a replay of `compute_statistics`'s calls.

        A replay would match by construction, whatever the two phases returned.
        """

        adapter = MockAdapter(_fixture())
        adapter.connect()
        columns = adapter.introspect_columns("public.curator_note")
        config = StatisticsConfig()

        counts, stats = adapter.compute_statistics(
            "public.curator_note",
            columns,
            config,
            frozenset(),
        )

        assert counts.row_count == 200
        assert counts.rows_scanned == 200
        assert stats["status"].cardinality == 3
        assert stats["status"].null_count == 0
        assert stats["field_notes"].cardinality == 100


class _RecordingAdapter(MockAdapter):
    """Records the suppression request and the order the phases were called in."""

    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.suppressed: set[str] = set()
        self.calls: list[str] = []

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, dict[str, BaseStats]]:
        self.calls.append("base")

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
        self.calls.append("columns")
        self.suppressed |= set(suppress_values)

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


def _profile(tmp_path: Path) -> dict[str, Any]:
    _generate(tmp_path)
    payload = yaml.safe_load(
        (tmp_path / "primary" / "public" / "curator_note" / "statistics.yaml").read_text(),
    )

    return payload["columns"]


def _generate(tmp_path: Path) -> None:
    Engine(MockAdapter(_fixture()), _conn_config(tmp_path), tmp_path).generate()


def _fixture() -> dict[str, MockTable]:
    """One table carrying the cases the exemption has to tell apart.

    `field_notes` and `phone` are prose and suppressed; `institution` (text, reporting `email`)
    and `status` (categorical, reporting prose) are not reached.
    """

    # A hundred distinct values over two hundred rows: above the enumeration threshold, so
    # the column classifies text, and the top-twenty list covers a fifth - hence long_tail.
    listed = tuple(ValueCount(value=f"v{i:02d}", count=2) for i in range(20))

    def text_stats() -> ColumnStats:
        return ColumnStats(
            sql_type="text",
            nullable=False,
            null_count=0,
            null_rate=0.0,
            cardinality=100,
            cardinality_ratio=0.5,
            cardinality_method="exact",
            values=listed,
            values_coverage=0.2,
            distribution="long_tail",
        )

    # Three values, enumerated in full, so this one's counts have to add up.
    status = ColumnStats(
        sql_type="text",
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=3,
        cardinality_ratio=0.015,
        cardinality_method="exact",
        values=(
            ValueCount(value="a", count=100),
            ValueCount(value="b", count=60),
            ValueCount(value="c", count=40),
        ),
        values_coverage=1.0,
        distribution="imbalanced",
    )

    return {
        "public.curator_note": MockTable(
            type="table",
            namespace_path=("public", "curator_note"),
            ddl=(
                "CREATE TABLE public.curator_note "
                "(field_notes text, institution text, status text, phone text);\n"
            ),
            columns=[
                ColumnMeta(
                    name="field_notes",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=1,
                ),
                ColumnMeta(
                    name="institution",
                    sql_type="text",
                    nullable=False,
                    default=None,
                    ordinal=2,
                ),
                ColumnMeta(name="status", sql_type="text", nullable=False, default=None, ordinal=3),
                ColumnMeta(name="phone", sql_type="text", nullable=False, default=None, ordinal=4),
            ],
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats={
                "field_notes": text_stats(),
                "institution": text_stats(),
                "status": status,
                "phone": text_stats(),
            },
            samples={
                "field_notes": PROSE,
                "institution": EMAILS,
                "status": PROSE,
                "phone": PROSE,
            },
            row_count=200,
        ),
    }
