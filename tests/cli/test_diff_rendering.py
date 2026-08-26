"""Unit tests for the dbprint diff renderers (text + structured)."""

from __future__ import annotations

import json
from io import StringIO
from typing import Any

import yaml

from dbprint.cli.rendering.diff_data import (
    DiffRenderOptions,
    render_data,
    render_human_text,
)


def _options(threshold_override: float | None = None) -> DiffRenderOptions:
    return DiffRenderOptions(
        thresholds={
            "cardinality_ratio": 0.02,
            "percentile_pct": 0.05,
            "values_coverage": 0.05,
            "default": 0.01,
        },
        threshold_override=threshold_override,
    )


def _empty_diff() -> dict[str, Any]:
    return {
        "format_version": 1,
        "connection": "primary",
        "adapter": "postgres",
        "changes": [],
        "summary": {},
    }


def _diff_with(changes: list[dict[str, Any]]) -> dict[str, Any]:
    base = _empty_diff()
    base["changes"] = changes

    return base


class TestEmptyDiff:
    def test_all_sections_show_none(self) -> None:
        text = render_human_text(_empty_diff(), _options())

        for label in (
            "Modified (DDL)",
            "Modified (row count)",
            "Modified (grain)",
            "Modified (physical layout)",
            "Modified (statistics)",
            "Modified (relationships)",
            "Modified (indexes)",
            "Added",
            "Removed",
        ):
            assert label in text

        # Every section renders the (none) marker on empty diff.
        assert text.count("(none)") == 9

    def test_footer_emitted(self) -> None:
        text = render_human_text(_empty_diff(), _options())
        assert "Run `dbprint generate` to refresh" in text


class TestDdlSection:
    def test_column_added_event_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "column_added",
                    "table": "public.curator",
                    "column": "email",
                    "sql_type": "varchar",
                    "nullable": True,
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "public.curator" in text
        assert "+ email" in text
        assert "varchar" in text

    def test_column_type_changed_shows_before_and_after(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "column_type_changed",
                    "table": "public.curator",
                    "column": "seed_count",
                    "before": "int",
                    "after": "bigint",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "~ seed_count" in text
        assert "'int'" in text
        assert "'bigint'" in text


class TestRowCountSection:
    def test_row_count_change_rendered_with_delta(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "table_row_count_changed",
                    "table": "public.curator",
                    "before": 100,
                    "after": 120,
                    "delta": 20,
                    "before_method": "exact",
                    "after_method": "exact",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "public.curator" in text
        assert "~ row_count: 100 -> 120 (+20)" in text
        assert "(approximate)" not in text

    def test_approximate_method_annotated(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "table_row_count_changed",
                    "table": "public.curator",
                    "before": 1_000_000,
                    "after": 1_050_000,
                    "delta": 50_000,
                    "before_method": "approximate",
                    "after_method": "approximate",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "(approximate)" in text

    def test_shrinking_shows_negative_delta(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "table_row_count_changed",
                    "table": "public.curator",
                    "before": 120,
                    "after": 100,
                    "delta": -20,
                    "before_method": "exact",
                    "after_method": "exact",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "(-20)" in text


class TestGrainSection:
    def test_a_declared_key_gained_is_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "grain_changed",
                    "table": "public.curator",
                    "before": {"keys": []},
                    "after": {"keys": [{"columns": ["id"], "detection": "declared"}]},
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "public.curator" in text
        assert "~ grain: none -> (id) declared" in text

    def test_search_exhausted_is_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "grain_changed",
                    "table": "public.curator",
                    "before": {"keys": [], "search": {"exhausted": False}},
                    "after": {"keys": [], "search": {"exhausted": True}},
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "[search exhausted=False]" in text
        assert "[search exhausted=True]" in text


class TestPhysicalLayoutSection:
    def test_a_genuine_gain_is_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "physical_layout_changed",
                    "table": "public.curation_event",
                    "before": None,
                    "after": {
                        "mechanism": "cluster",
                        "keys": [{"expression": "logged_at::date"}],
                    },
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "public.curation_event" in text
        assert "~ physical_layout: none -> cluster (logged_at::date)" in text


class TestStatisticsThreshold:
    def test_sub_threshold_filtered_in_human(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "id",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 1.005,
                    "delta": 0.005,
                    "delta_pct": 0.005,  # below 0.02 threshold for cardinality_ratio
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "Modified (statistics):\n  (none)" in text

    def test_above_threshold_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "id",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 0.95,
                    "delta": -0.05,
                    "delta_pct": -0.05,  # above 0.02 threshold
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "Modified (statistics):" in text
        assert "id cardinality_ratio" in text

    def test_a_configured_threshold_decides_the_line(self) -> None:
        """The renderer judges by the dict it is handed, not by the spec defaults."""

        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "id",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 0.9,
                    "delta": -0.1,
                    "delta_pct": -0.1,  # between the two thresholds below
                },
            ],
        )
        coarse = DiffRenderOptions(thresholds={"cardinality_ratio": 0.5, "default": 0.01})
        fine = DiffRenderOptions(thresholds={"cardinality_ratio": 0.05, "default": 0.01})

        assert "id cardinality_ratio" not in render_human_text(diff, coarse)
        assert "id cardinality_ratio" in render_human_text(diff, fine)

    def test_override_zero_shows_every_event(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "id",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 1.0001,
                    "delta": 0.0001,
                    "delta_pct": 0.0001,
                },
            ],
        )
        text = render_human_text(diff, _options(threshold_override=0.0))
        assert "id cardinality_ratio" in text

    def test_event_without_delta_pct_always_rendered(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "public.curator",
                    "column": "status",
                    "stat": "classification",
                    "before": "categorical",
                    "after": "text",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "status classification" in text


class TestRelationshipsDualDirection:
    def test_added_renders_under_both_source_and_target(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "relationship_added",
                    "source_table": "public.curator",
                    "source_column": ["herbarium_id"],
                    "target_table": "public.herbarium",
                    "target_column": ["id"],
                    "on_delete": "CASCADE",
                    "on_update": "NO ACTION",
                    "detection": "declared",
                },
            ],
        )
        text = render_human_text(diff, _options())
        # Under source: outgoing -> target
        assert "+ -> public.herbarium.id" in text
        # Under target: incoming <- source
        assert "+ <- public.curator.herbarium_id" in text

    def test_modified_includes_on_delete_change(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "relationship_modified",
                    "source_table": "public.curator",
                    "source_column": ["herbarium_id"],
                    "target_table": "public.herbarium",
                    "target_column": ["id"],
                    "on_delete": {"before": "NO ACTION", "after": "CASCADE"},
                    "on_update": "NO ACTION",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "on_delete NO ACTION -> CASCADE" in text


class TestIndexesSection:
    def test_index_added(self) -> None:
        diff = _diff_with(
            [
                {
                    "kind": "index_added",
                    "table": "public.curator",
                    "index_name": "curator_email_idx",
                    "columns": ["email"],
                    "unique": True,
                    "type": "btree",
                },
            ],
        )
        text = render_human_text(diff, _options())
        assert "+ curator_email_idx" in text
        assert "(email)" in text
        assert "unique=True" in text


class TestAddedRemovedSections:
    def test_table_added_lists_fqn_and_type(self) -> None:
        diff = _diff_with(
            [{"kind": "table_added", "table": "public.curation_event", "type": "table"}],
        )
        text = render_human_text(diff, _options())
        assert "Added:" in text
        assert "public.curation_event" in text

    def test_table_removed_lists_fqn(self) -> None:
        diff = _diff_with([{"kind": "table_removed", "table": "public.legacy"}])
        text = render_human_text(diff, _options())
        assert "Removed:" in text
        assert "public.legacy" in text


class TestStructuredOutput:
    def test_yaml_is_multidoc(self) -> None:
        diffs = [_empty_diff(), {**_empty_diff(), "connection": "secondary"}]
        buf = StringIO()
        render_data(diffs, "yaml", buf)
        docs = list(yaml.safe_load_all(buf.getvalue()))
        assert len(docs) == 2
        assert docs[0]["connection"] == "primary"
        assert docs[1]["connection"] == "secondary"

    def test_json_is_array(self) -> None:
        diffs = [_empty_diff(), {**_empty_diff(), "connection": "secondary"}]
        buf = StringIO()
        render_data(diffs, "json", buf)
        data = json.loads(buf.getvalue())
        assert isinstance(data, list)
        assert {d["connection"] for d in data} == {"primary", "secondary"}

    def test_structured_output_ignores_threshold(self) -> None:
        # Even a tiny sub-threshold drift survives in machine output.
        diff = _diff_with(
            [
                {
                    "kind": "statistic_changed",
                    "table": "t",
                    "column": "x",
                    "stat": "cardinality_ratio",
                    "before": 1.0,
                    "after": 1.0001,
                    "delta": 0.0001,
                    "delta_pct": 0.0001,
                },
            ],
        )
        buf = StringIO()
        render_data([diff], "json", buf)
        data = json.loads(buf.getvalue())
        assert data[0]["changes"][0]["kind"] == "statistic_changed"
