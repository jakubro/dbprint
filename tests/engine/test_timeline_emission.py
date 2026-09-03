"""What the engine writes and renders for a table's `timeline` (SPEC 2.2.16) - the grouped
statement belongs to the adapters; these cover the anchor rule, adaptive unit and skips.
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
    PhysicalLayout,
    PhysicalLayoutKey,
    Range,
)
from dbprint.config import ConnectionConfig, RuleConfig
from dbprint.config.project import RedactRule
from dbprint.engine import Engine
from dbprint.engine.context_assembler import AssemblyOptions, assemble


class TestAnchorSelection:
    def test_physical_layout_key_wins_over_lower_null_rate(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200), "clustered_at": ("timestamp", 500, 200)},
            physical_layout=PhysicalLayout(
                mechanism="cluster",
                keys=(PhysicalLayoutKey(expression="clustered_at", column="clustered_at"),),
            ),
            timeline_buckets={"clustered_at": (("2024-01-01T00:00:00", 5),)},
        )

        assert payload["timeline"]["column"] == "clustered_at"

    def test_lowest_null_rate_wins_among_eligible_columns(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 100, 200), "updated_at": ("timestamp", 50, 200)},
            timeline_buckets={"updated_at": (("2024-01-01T00:00:00", 5),)},
        )

        assert payload["timeline"]["column"] == "updated_at"

    def test_ties_break_on_higher_cardinality(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"a_col": ("timestamp", 0, 100), "b_col": ("timestamp", 0, 300)},
            timeline_buckets={"b_col": (("2024-01-01T00:00:00", 5),)},
        )

        assert payload["timeline"]["column"] == "b_col"

    def test_ties_break_lexicographically_on_column_name(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"zeta": ("timestamp", 0, 200), "alpha": ("timestamp", 0, 200)},
            timeline_buckets={"alpha": (("2024-01-01T00:00:00", 5),)},
        )

        assert payload["timeline"]["column"] == "alpha"

    def test_a_redacted_temporal_column_is_never_chosen(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"secret_at": ("timestamp", 0, 200)},
            redact=(RedactRule(columns=("*.secret_at",), with_="mask"),),
        )

        assert "timeline" not in payload

    def test_a_time_only_column_never_qualifies(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, {"clock_only": ("time", 0, 200)})

        assert "timeline" not in payload

    def test_no_temporal_column_means_absent(self, tmp_path: Path) -> None:
        payload = _generate(tmp_path, {"name": ("text", 0, 300)})

        assert "timeline" not in payload


class TestUnitSelection:
    def test_a_short_span_buckets_by_day(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            spans={"created_at": 30},
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 5),)},
        )

        assert payload["timeline"]["unit"] == "day"

    def test_span_of_exactly_90_days_still_buckets_by_day(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            spans={"created_at": 90},
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 5),)},
        )

        assert payload["timeline"]["unit"] == "day"

    def test_span_of_91_days_widens_to_week(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            spans={"created_at": 91},
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 5),)},
        )

        assert payload["timeline"]["unit"] == "week"

    def test_span_of_exactly_730_days_still_buckets_by_week(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            spans={"created_at": 730},
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 5),)},
        )

        assert payload["timeline"]["unit"] == "week"

    def test_span_of_731_days_widens_to_month(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            spans={"created_at": 731},
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 5),)},
        )

        assert payload["timeline"]["unit"] == "month"

    def test_an_unmeasured_span_defaults_to_day(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            spans={"created_at": None},
            timeline_buckets={},
        )

        assert payload["timeline"]["unit"] == "day"


class TestBucketsAndCoverage:
    def test_buckets_echo_the_probe_verbatim_and_ascending(self, tmp_path: Path) -> None:
        buckets = (("2024-01-01T00:00:00", 10), ("2024-03-01T00:00:00", 5))
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            row_count=100,
            timeline_buckets={"created_at": buckets},
        )

        assert payload["timeline"]["buckets"] == [
            {"start": "2024-01-01T00:00:00", "count": 10},
            {"start": "2024-03-01T00:00:00", "count": 5},
        ]

    def test_coverage_is_the_listed_counts_over_rows_scanned(self, tmp_path: Path) -> None:
        buckets = (("2024-01-01T00:00:00", 30), ("2024-02-01T00:00:00", 20))
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            row_count=100,
            timeline_buckets={"created_at": buckets},
        )

        assert payload["timeline"]["coverage"] == 0.5

    def test_no_buckets_from_the_probe_is_a_present_empty_list(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            row_count=100,
            timeline_buckets={},
        )

        assert payload["timeline"]["buckets"] == []
        assert payload["timeline"]["coverage"] == 0.0

    def test_coverage_rounds_a_long_decimal_tail_to_six_places(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            row_count=7,
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 6),)},
        )

        assert payload["timeline"]["coverage"] == 0.857143

    def test_a_fully_covered_anchor_reads_exactly_one_not_the_clamp(
        self,
        tmp_path: Path,
    ) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            row_count=100,
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 100),)},
        )

        assert payload["timeline"]["coverage"] == 1.0

    def test_covered_exceeding_rows_scanned_clamps_below_one(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            row_count=100,
            rows_scanned=90,
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 100),)},
        )

        assert payload["timeline"]["coverage"] == 0.999999


class TestSkipConditions:
    def test_scope_suppresses_the_block(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 5),)},
            sample=0.5,
        )

        assert "timeline" not in payload

    def test_an_empty_table_suppresses_the_block(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            row_count=0,
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 5),)},
        )

        assert "timeline" not in payload

    def test_compute_timeline_false_suppresses_the_block(self, tmp_path: Path) -> None:
        payload = _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 5),)},
            compute_timeline=False,
        )

        assert "timeline" not in payload


class TestContextRendering:
    def test_the_summary_line_names_anchor_unit_bucket_count_span_and_coverage(
        self,
        tmp_path: Path,
    ) -> None:
        text = _context(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            timeline_buckets={
                "created_at": (("2024-01-01T00:00:00", 10), ("2024-02-01T00:00:00", 5)),
            },
            row_count=100,
        )

        assert (
            "Timeline: created_at (month), 2 bucket(s), "
            "2024-01-01T00:00:00 to 2024-02-01T00:00:00, 15.0% of scanned rows" in text
        )

    def test_no_anchor_says_nothing(self, tmp_path: Path) -> None:
        text = _context(tmp_path, {"name": ("text", 0, 300)})

        assert "Timeline:" not in text


class TestStructuredFormats:
    def test_the_structured_yaml_carries_the_block_whole(self, tmp_path: Path) -> None:
        _generate(
            tmp_path,
            {"created_at": ("timestamp", 0, 200)},
            timeline_buckets={"created_at": (("2024-01-01T00:00:00", 5),)},
        )
        root = tmp_path / "w"
        manifest = yaml.safe_load((root / "manifest.yaml").read_text())
        rendered = yaml.safe_load(
            assemble(
                manifest,
                root,
                ["public.wide"],
                AssemblyOptions(format="yaml", include_ddl=False),
            ).text,
        )

        assert rendered["statistics"]["timeline"]["column"] == "created_at"
        assert rendered["statistics"]["timeline"]["buckets"] == [
            {"start": "2024-01-01T00:00:00", "count": 5},
        ]


def _generate(
    tmp_path: Path,
    columns_spec: dict[str, tuple[str, int, int]],
    *,
    row_count: int = 1000,
    rows_scanned: int | None = None,
    timeline_buckets: dict[str, tuple[tuple[str, int], ...]] | None = None,
    physical_layout: PhysicalLayout | None = None,
    spans: dict[str, int | None] | None = None,
    redact: tuple[RedactRule, ...] = (),
    sample: float | None = None,
    compute_timeline: bool = True,
) -> dict[str, Any]:
    rules = (RuleConfig(sample=sample),) if sample is not None else ()
    conn = ConnectionConfig(
        name="w",
        adapter="postgres",
        output=tmp_path,
        infer_relationships=False,
        redact=redact,
        rules=rules,
        compute_timeline=compute_timeline,
    )
    fixture = _fixture(
        columns_spec,
        row_count=row_count,
        rows_scanned=rows_scanned,
        timeline_buckets=timeline_buckets,
        physical_layout=physical_layout,
        spans=spans,
    )
    Engine(MockAdapter(fixture), conn, tmp_path).generate()

    return yaml.safe_load((tmp_path / "w" / "public" / "wide" / "statistics.yaml").read_text())


def _context(
    tmp_path: Path,
    columns_spec: dict[str, tuple[str, int, int]],
    **kwargs: Any,
) -> str:
    _generate(tmp_path, columns_spec, **kwargs)
    root = tmp_path / "w"
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())

    return assemble(
        manifest,
        root,
        ["public.wide"],
        AssemblyOptions(format="md", include_ddl=False),
    ).text


# Temporal raw types the anchor rule accepts calendar bucketing for. "time" rides one test
# to prove the clock-only exclusion; every other spec here uses "timestamp".
_TEMPORAL_TYPES = frozenset({"timestamp", "date", "time"})


def _fixture(
    columns_spec: dict[str, tuple[str, int, int]],
    *,
    row_count: int,
    rows_scanned: int | None,
    timeline_buckets: dict[str, tuple[tuple[str, int], ...]] | None,
    physical_layout: PhysicalLayout | None,
    spans: dict[str, int | None] | None,
) -> dict[str, MockTable]:
    spans = spans or {}

    def _column(
        sql_type: str,
        null_count: int,
        cardinality: int,
        span_days: int | None,
    ) -> ColumnStats:
        is_temporal = sql_type in _TEMPORAL_TYPES

        return ColumnStats(
            sql_type=sql_type,
            nullable=null_count > 0,
            null_count=null_count,
            null_rate=null_count / row_count if row_count else 0.0,
            cardinality=cardinality,
            cardinality_ratio=cardinality / row_count if row_count else 0.0,
            cardinality_method="exact",
            range=(
                Range(min="2024-01-01T00:00:00Z", max="2024-02-01T00:00:00Z", span_days=span_days)
                if is_temporal
                else None
            ),
        )

    names = list(columns_spec)
    columns = [
        ColumnMeta(
            name=name,
            sql_type=columns_spec[name][0],
            nullable=columns_spec[name][1] > 0,
            default=None,
            ordinal=i,
        )
        for i, name in enumerate(names, start=1)
    ]
    stats = {
        name: _column(sql_type, null_count, cardinality, spans.get(name, 900))
        for name, (sql_type, null_count, cardinality) in columns_spec.items()
    }

    return {
        "public.wide": MockTable(
            type="table",
            namespace_path=("public", "wide"),
            ddl="CREATE TABLE public.wide (placeholder text);\n",
            columns=columns,
            relationships=[],
            indexes=[],
            comments=CommentsMeta(table=None, columns={}),
            stats=stats,
            samples={},
            row_count=row_count,
            rows_scanned=rows_scanned,
            physical_layout=physical_layout,
            timeline_buckets=timeline_buckets or {},
        ),
    }
