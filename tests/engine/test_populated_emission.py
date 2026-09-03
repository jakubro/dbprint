"""What the engine writes and renders for a column's `populated` window (SPEC 2.2.4) - the
grouped statement belongs to the adapters; these cover the anchor reuse, suppression and skips.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.adapters import (
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    MockAdapter,
    MockTable,
    Range,
)
from dbprint.config import ConnectionConfig, RuleConfig
from dbprint.engine import Engine
from dbprint.engine.context_assembler import AssemblyOptions, assemble


class TestAnchorReuse:
    def test_absent_when_compute_timeline_is_disabled(self, tmp_path: Path) -> None:
        """`populated` is gated on `timeline`'s own presence, not re-derived independently."""

        payload = _generate(tmp_path, compute_timeline=False)

        assert "populated" not in payload["columns"]["added_later"]

    def test_absent_under_scope(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, sample=0.5)

        assert "populated" not in payload["columns"]["added_later"]

    def test_absent_with_no_anchor_column(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, anchor_sql_type="text")

        assert "populated" not in payload["columns"]["added_later"]


class TestSuppression:
    def test_a_fully_populated_column_carries_no_field(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, added_later_null_count=0)

        assert "populated" not in payload["columns"]["added_later"]

    def test_an_all_null_column_carries_no_field(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, added_later_null_count=200)

        assert "populated" not in payload["columns"]["added_later"]

    def test_an_eligible_column_carries_its_stated_window(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path)

        assert payload["columns"]["added_later"]["populated"] == {
            "from": "2026-03-04T00:00:00",
            "to": "2026-08-27T00:00:00",
        }


class TestContentAndConsistency:
    def test_two_eligible_columns_publish_distinguishable_windows(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path)

        added_later = payload["columns"]["added_later"]["populated"]
        abandoned = payload["columns"]["abandoned"]["populated"]

        assert added_later != abandoned

    def test_the_anchor_itself_is_never_a_subject_when_fully_populated(
        self,
        tmp_path: Path,
    ) -> None:
        payload = _generate(tmp_path)

        assert "populated" not in payload["columns"]["created_at"]


class TestContextRendering:
    def test_the_notes_cell_states_the_window(self, tmp_path: Path) -> None:
        text = _context(tmp_path)

        assert "populated 2026-03-04T00:00:00 to 2026-08-27T00:00:00" in text

    def test_a_fully_populated_column_carries_no_populated_suffix(self, tmp_path: Path) -> None:
        text = _context(tmp_path, added_later_null_count=0)

        assert "2026-03-04T00:00:00 to 2026-08-27T00:00:00" not in text


class TestStructuredFormats:
    def test_the_structured_yaml_carries_the_field_whole(self, tmp_path: Path) -> None:
        _generate(tmp_path)
        root = tmp_path / "w"
        manifest = yaml.safe_load((root / "manifest.yaml").read_text())
        rendered = yaml.safe_load(
            assemble(
                manifest,
                root,
                ["fixture.backfilled"],
                AssemblyOptions(format="yaml", include_ddl=False),
            ).text,
        )

        assert rendered["statistics"]["columns"]["added_later"]["populated"] == {
            "from": "2026-03-04T00:00:00",
            "to": "2026-08-27T00:00:00",
        }


def _generate(
    tmp_path: Path,
    *,
    row_count: int = 200,
    added_later_null_count: int = 40,
    anchor_sql_type: str = "timestamp",
    sample: float | None = None,
    compute_timeline: bool = True,
) -> dict[str, Any]:
    rules = (RuleConfig(sample=sample),) if sample is not None else ()
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
        rules=rules,
        compute_timeline=compute_timeline,
    )
    fixture = _fixture(
        row_count=row_count,
        added_later_null_count=added_later_null_count,
        anchor_sql_type=anchor_sql_type,
    )
    Engine(MockAdapter(fixture), conn, tmp_path).generate()

    return yaml.safe_load(
        (tmp_path / "w" / "fixture" / "backfilled" / "statistics.yaml").read_text(),
    )


def _context(tmp_path: Path, **kwargs: Any) -> str:
    _generate(tmp_path, **kwargs)
    root = tmp_path / "w"
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())

    return assemble(
        manifest,
        root,
        ["fixture.backfilled"],
        AssemblyOptions(format="md", include_ddl=False),
    ).text


def _fixture(
    *,
    row_count: int,
    added_later_null_count: int,
    anchor_sql_type: str,
) -> dict[str, MockTable]:
    is_temporal = anchor_sql_type == "timestamp"
    created_at = ColumnStats(
        sql_type=anchor_sql_type,
        nullable=False,
        null_count=0,
        null_rate=0.0,
        cardinality=200,
        cardinality_ratio=1.0,
        cardinality_method="exact",
        range=(
            Range(min="2024-01-01T00:00:00Z", max="2026-08-27T00:00:00Z", span_days=970)
            if is_temporal
            else None
        ),
    )

    def subject(null_count: int) -> ColumnStats:
        return ColumnStats(
            sql_type="text",
            nullable=null_count > 0,
            null_count=null_count,
            null_rate=null_count / row_count if row_count else 0.0,
            cardinality=200,
            cardinality_ratio=200 / row_count if row_count else 0.0,
            cardinality_method="exact",
        )

    table = MockTable(
        type="table",
        namespace_path=("fixture", "backfilled"),
        ddl=(
            "CREATE TABLE fixture.backfilled (created_at timestamp, added_later text, "
            "abandoned text);\n"
        ),
        columns=[
            ColumnMeta(
                name="created_at",
                sql_type=anchor_sql_type,
                nullable=False,
                default=None,
                ordinal=1,
            ),
            ColumnMeta(name="added_later", sql_type="text", nullable=True, default=None, ordinal=2),
            ColumnMeta(name="abandoned", sql_type="text", nullable=True, default=None, ordinal=3),
        ],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "created_at": created_at,
            "added_later": subject(added_later_null_count),
            "abandoned": subject(40),
        },
        samples={},
        row_count=row_count,
        timeline_buckets={"created_at": (("2024-01-01T00:00:00", row_count),)}
        if is_temporal
        else {},
        populated_windows={
            "added_later": ("2026-03-04T00:00:00", "2026-08-27T00:00:00"),
            "abandoned": ("2024-01-01T00:00:00", "2024-06-01T00:00:00"),
        },
    )

    return {"fixture.backfilled": table}
