"""A prose column pays for no enumeration (SPEC 2.2.3).

The saving is the grouped scan, not the bytes, so the engine classifies from Phase A and
tells Phase B which value lists not to build. Both the artifact and the request are
asserted: an adapter that ignored the request would still produce a conforming print.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dbprint.adapters import (
    BaseStats,
    ColumnMeta,
    MockAdapter,
    MockTable,
    StatisticsConfig,
    TableCounts,
    TableScope,
)
from dbprint.adapters.base import ColumnProgress, PhaseB
from dbprint.engine import Engine
from tests._curator import conn_config
from tests._engine_run import artifact, conformance_errors
from tests.engine._prose import prose_fixture


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
        errors = conformance_errors(tmp_path / "primary")

        assert errors == [], "\n".join(f"  {e.code} at {e.path}: {e.detail}" for e in errors)


class TestTheEngineAsksForTheSkip:
    """The artifact cannot distinguish a skipped scan from a discarded result."""

    def test_only_the_prose_columns_are_suppressed(self, tmp_path: Path) -> None:
        adapter = _RecordingAdapter(prose_fixture())
        Engine(adapter, conn_config(tmp_path), tmp_path).generate()

        assert adapter.suppressed == {"field_notes", "phone"}


class _RecordingAdapter(MockAdapter):
    """Records which columns the engine asked Phase B to skip the value scan for."""

    def __init__(self, fixture: dict[str, MockTable]) -> None:
        super().__init__(fixture)
        self.suppressed: set[str] = set()

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
    ) -> PhaseB:
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
    payload = artifact(tmp_path / "primary", "public.curator_note")

    return payload["columns"]


def _generate(tmp_path: Path) -> None:
    Engine(MockAdapter(prose_fixture()), conn_config(tmp_path), tmp_path).generate()
