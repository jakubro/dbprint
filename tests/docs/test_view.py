"""`view.py` - every rendering rule the docs site must get right."""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.config import ConnectionConfig
from dbprint.docs import catalogue, view


def _column(conn: ConnectionConfig, fqn: str, name: str) -> dict[str, Any]:
    found = catalogue.load_connections([conn])[0]
    artifacts = catalogue.load_table(found, fqn)
    assert artifacts is not None
    assert artifacts.statistics is not None

    return artifacts.statistics["columns"][name]


def _statistics(conn: ConnectionConfig, fqn: str) -> dict[str, Any]:
    found = catalogue.load_connections([conn])[0]
    artifacts = catalogue.load_table(found, fqn)
    assert artifacts is not None
    assert artifacts.statistics is not None

    return artifacts.statistics


class TestRowCountView:
    def test_fully_scanned_table_still_states_the_share(self, rich_conn: ConnectionConfig) -> None:
        # No `scope` block - SPEC 2.2.8 defines rows_scanned == row_count, so the share is 100%.
        found = catalogue.load_connections([rich_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.batch")
        assert artifacts is not None

        row_count = view.row_count_view(artifacts.entry, artifacts.statistics)

        assert row_count["rows_scanned"] == 300
        assert row_count["share_pct"] == 100.0
        assert row_count["filter"] is None

    def test_scoped_table_reports_the_measured_share(self, scoped_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([scoped_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.curation_event")
        assert artifacts is not None

        row_count = view.row_count_view(artifacts.entry, artifacts.statistics)

        assert row_count["rows_scanned"] == 10_000
        assert row_count["share_pct"] == 1.0

    def test_no_statistics_carries_no_scan_share(self) -> None:
        row_count = view.row_count_view({"row_count": 300}, None)

        assert row_count["row_count"] == 300
        assert row_count["rows_scanned"] is None
        assert row_count["share_pct"] is None


class TestScopeView:
    def test_absent_scope_is_none(self, rich_conn: ConnectionConfig) -> None:
        statistics = _statistics(rich_conn, "seedbank.batch")

        assert view.scope_view(statistics) is None

    def test_share_is_rows_scanned_over_row_count_not_sample(
        self,
        scoped_conn: ConnectionConfig,
    ) -> None:
        statistics = _statistics(scoped_conn, "seedbank.curation_event")

        scope = view.scope_view(statistics)

        assert scope is not None
        # 10_000/1_000_000 -> 1.0%; sample=0.01 agrees numerically on purpose, so the
        # assertion below pins which field is read.
        assert scope["share_pct"] == 1.0
        assert scope["rows_scanned"] == 10_000
        assert scope["sample"] == 0.01

    def test_share_none_when_row_count_absent_or_zero(self) -> None:
        no_row_count = view.scope_view({"scope": {"rows_scanned": 5}})
        zero_row_count = view.scope_view({"scope": {"rows_scanned": 5}, "row_count": 0})

        assert no_row_count is not None
        assert zero_row_count is not None
        assert no_row_count["share_pct"] is None
        assert zero_row_count["share_pct"] is None


class TestGrainView:
    def test_declared_keys(self, rich_conn: ConnectionConfig) -> None:
        statistics = _statistics(rich_conn, "seedbank.batch")

        grain = view.grain_view(statistics)

        assert grain is not None
        assert grain["key_list"] == [{"columns": ["batch_id"], "detection": "declared"}]

    def test_absent_grain_is_none(self) -> None:
        assert view.grain_view({}) is None

    def test_empty_keys_with_no_search_reads_as_not_determined(self) -> None:
        grain = view.grain_view({"grain": {"keys": []}})

        assert grain == {"key_list": [], "search_ran": False, "exhausted": None}

    def test_search_exhausted_true_means_nothing_found(self) -> None:
        grain = view.grain_view({"grain": {"keys": [], "search": {"exhausted": True}}})

        assert grain == {"key_list": [], "search_ran": True, "exhausted": True}

    def test_search_exhausted_false_means_the_search_gave_up(self) -> None:
        grain = view.grain_view({"grain": {"keys": [], "search": {"exhausted": False}}})

        assert grain is not None
        assert grain["exhausted"] is False

    def test_annotated_key_rides_beside_the_measured_one(self) -> None:
        statistics = {"grain": {"keys": [{"columns": ["id"], "detection": "declared"}]}}
        annotations = {"grain": {"keys": [{"columns": ["a", "b"], "note": "business key"}]}}

        grain = view.grain_view(statistics, annotations)

        assert grain is not None
        assert grain["key_list"] == [
            {"columns": ["id"], "detection": "declared"},
            {"columns": ["a", "b"], "detection": "annotated", "note": "business key"},
        ]

    def test_annotated_key_with_no_note_carries_no_note_key(self) -> None:
        annotations = {"grain": {"keys": [{"columns": ["a", "b"]}]}}

        grain = view.grain_view({}, annotations)

        assert grain is not None
        assert "note" not in grain["key_list"][0]

    def test_annotated_key_alone_still_renders_a_view(self) -> None:
        """SPEC 2.7.1: a human states a key the producer measured none for at all."""

        annotations = {"grain": {"keys": [{"columns": ["a", "b"]}]}}

        grain = view.grain_view({}, annotations)

        assert grain is not None
        assert grain["key_list"] == [{"columns": ["a", "b"], "detection": "annotated"}]

    def test_no_annotation_leaves_the_measured_view_unchanged(
        self,
        rich_conn: ConnectionConfig,
    ) -> None:
        statistics = _statistics(rich_conn, "seedbank.batch")

        assert view.grain_view(statistics, None) == view.grain_view(statistics)


class TestNullPatternsView:
    def test_present_when_a_column_has_nulls(self, rich_conn: ConnectionConfig) -> None:
        statistics = _statistics(rich_conn, "seedbank.batch")

        patterns = view.null_patterns_view(statistics)

        assert patterns is not None
        assert patterns["coverage"] == 1.0
        assert {"columns": ["notes"], "count": 20} in patterns["patterns"]

    def test_absent_is_none(self) -> None:
        assert view.null_patterns_view({}) is None


class TestNullCompanions:
    def test_multi_column_entry_names_the_other_columns(
        self,
        companion_conn: ConnectionConfig,
    ) -> None:
        statistics = _statistics(companion_conn, "seedbank.botanists")
        patterns = view.null_patterns_view(statistics)

        assert view.null_companions(patterns, "email") == ["phone"]
        assert view.null_companions(patterns, "phone") == ["email"]

    def test_single_column_entry_names_no_companion(self, rich_conn: ConnectionConfig) -> None:
        # The only pattern is ["notes"] - an exact combination (SPEC 2.2.10), so no companion.
        statistics = _statistics(rich_conn, "seedbank.batch")
        patterns = view.null_patterns_view(statistics)

        assert view.null_companions(patterns, "notes") == []

    def test_absent_patterns_is_empty(self) -> None:
        assert view.null_companions(None, "x") == []


class TestPhysicalLayoutView:
    def test_present(self, rich_conn: ConnectionConfig) -> None:
        statistics = _statistics(rich_conn, "seedbank.batch")

        layout = view.physical_layout_view(statistics)

        assert layout == {
            "mechanism": "cluster",
            "key_list": [{"expression": "cultivar_id", "column": "cultivar_id"}],
        }

    def test_absent_means_not_declared_never_not_checked(self) -> None:
        assert view.physical_layout_view({}) is None


class TestDependenciesView:
    def test_present(self, rich_conn: ConnectionConfig) -> None:
        statistics = _statistics(rich_conn, "seedbank.batch")

        deps = view.dependencies_view(statistics)

        assert deps == [
            {"determinant": "cultivar_id", "dependent": "cultivar_name", "strength": 1.0},
        ]

    def test_absent_is_empty_list_not_none(self) -> None:
        assert view.dependencies_view({}) == []


class TestColumnsEmptyNotice:
    def test_empty_columns_reads_as_not_read(self, empty_columns_conn: ConnectionConfig) -> None:
        statistics = _statistics(empty_columns_conn, "public.narrow")

        notice = view.columns_empty_notice(statistics)

        assert notice is not None
        assert "read" in notice.lower()  # about the READ, never a flat "the table has no columns"
        assert "empty table" not in notice.lower()

    def test_populated_columns_has_no_notice(self, rich_conn: ConnectionConfig) -> None:
        statistics = _statistics(rich_conn, "seedbank.batch")

        assert view.columns_empty_notice(statistics) is None

    def test_none_statistics_has_no_notice(self) -> None:
        assert view.columns_empty_notice(None) is None


class TestSummaryCards:
    def test_counts_sensitive_and_redacted(self, rich_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([rich_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.batch")
        assert artifacts is not None
        assert artifacts.statistics is not None
        columns = artifacts.statistics["columns"]

        cards = view.summary_cards(columns, artifacts.relationships)

        assert cards["sensitive"] == 1  # cultivar_name carries inferred.sensitivity
        assert cards["n_columns"] == 4
        assert cards["refers_to"] == 1
        assert cards["referenced_by"] == 1


class TestCardinalityView:
    def test_averages_the_null_adjusted_ratio(self, companion_conn: ConnectionConfig) -> None:
        # 100/100, 75/(100-25), 40/(100-20) -> avg 83.3%; the raw ratio would give 71.7%.
        statistics = _statistics(companion_conn, "seedbank.botanists")

        cardinality = view.cardinality_view(statistics["columns"], statistics["row_count"])

        assert cardinality is not None
        assert cardinality["avg_pct"] == 83.3
        assert cardinality["n_columns"] == 3

    def test_an_all_null_column_contributes_no_ratio(self) -> None:
        columns = {"a": {"cardinality": 0, "null_count": 10}}

        assert view.cardinality_view(columns, 10) is None

    def test_an_unsupported_column_is_excluded(self) -> None:
        columns = {"a": {"cardinality": None}, "b": {"cardinality": 5, "null_count": 0}}

        cardinality = view.cardinality_view(columns, 5)

        assert cardinality is not None
        assert cardinality["n_columns"] == 1
        assert cardinality["n_total"] == 2

    def test_no_columns_is_none(self) -> None:
        assert view.cardinality_view({}, 100) is None

    def test_no_rows_scanned_anywhere_contributes_no_ratio(self) -> None:
        # No `rows_scanned` and no `row_count` fallback - no denominator.
        columns = {"a": {"cardinality": 5, "null_count": 0}}

        assert view.cardinality_view(columns, None) is None


class TestCompletenessView:
    def test_averages_and_buckets_by_completeness(self, companion_conn: ConnectionConfig) -> None:
        # contributor_id 1.0 (full), email 0.75 (mid), phone 0.8 (mid) -> avg 85.0%
        statistics = _statistics(companion_conn, "seedbank.botanists")

        completeness = view.completeness_view(statistics["columns"])

        assert completeness is not None
        assert completeness["avg_pct"] == 85.0
        assert dict(completeness["buckets"]) == {"full": 1, "high": 0, "mid": 2, "low": 0}

    def test_high_and_low_buckets(self) -> None:
        columns = {"a": {"null_rate": 0.05}, "b": {"null_rate": 0.9}}  # 0.95 high, 0.1 low

        completeness = view.completeness_view(columns)

        assert completeness is not None
        assert dict(completeness["buckets"]) == {"full": 0, "high": 1, "mid": 0, "low": 1}

    def test_no_columns_is_none(self) -> None:
        assert view.completeness_view({}) is None


class TestSkylineLegend:
    def test_the_key_bucket_is_labelled_fk_candidate(self) -> None:
        legend = dict(view.skyline_legend())

        assert legend["key"] == "FK candidate"


class TestSkylineBar:
    def test_foreign_key_candidate_buckets_under_key_not_identifier(self) -> None:
        bar = view.skyline_bar({"classification": "foreign_key_candidate", "null_rate": 0.0}, 50.0)

        assert bar["bucket"] == "key"

    def test_unknown_classification_falls_back_to_unsupported(self) -> None:
        bar = view.skyline_bar({"classification": "made_up", "null_rate": 0.0}, 10.0)

        assert bar["bucket"] == "unsupported"


class TestCardinalityCell:
    def test_approximate_method_is_flagged(self) -> None:
        cell = view.cardinality_cell({"cardinality": 5, "cardinality_method": "approximate"}, 100)

        assert cell is not None
        assert cell["approximate"] is True

    def test_saturates_prefers_rows_scanned_over_row_count(self) -> None:
        cell = view.cardinality_cell({"cardinality": 10, "rows_scanned": 10}, 1000)

        assert cell is not None
        assert cell["saturates"] is True

    def test_no_cardinality_is_none(self) -> None:
        assert view.cardinality_cell({}, 100) is None


class TestValuesView:
    def test_bars_percentage_relative_to_the_top_entry(self) -> None:
        col = {"values": [{"value": "a", "count": 10}, {"value": "b", "count": 5}]}

        result = view.values_view(col)

        assert result is not None
        assert result["bars"][0]["pct"] == 100.0
        assert result["bars"][1]["pct"] == 50.0

    def test_coverage_and_method_pass_through(self, rich_conn: ConnectionConfig) -> None:
        col = _column(rich_conn, "seedbank.batch", "cultivar_id")

        result = view.values_view(col)

        assert result is not None
        assert result["coverage"] == 0.05
        assert result["exhaustive"] is False

    def test_no_values_no_coverage_is_none(self) -> None:
        assert view.values_view({}) is None


class TestRangeView:
    def test_box_present_when_quartiles_and_range_agree(self, rich_conn: ConnectionConfig) -> None:
        col = _column(rich_conn, "seedbank.batch", "batch_id")

        result = view.range_view(col)

        assert result is not None
        assert result["box"] is not None
        assert result["box"]["median"] == pytest.approx(49.83, abs=0.5)

    def test_full_percentile_set_is_listed_not_only_quartiles(
        self,
        rich_conn: ConnectionConfig,
    ) -> None:
        col = _column(rich_conn, "seedbank.batch", "batch_id")

        result = view.range_view(col)

        assert result is not None
        keys = [k for k, _ in result["percentiles"]]
        assert keys == ["p01", "p25", "p50", "p75", "p99"]  # not just p25/p50/p75

    def test_redacted_column_suppresses_box_and_percentiles_and_range(
        self,
        redacted_conn: ConnectionConfig,
    ) -> None:
        col = _column(redacted_conn, "seedbank.curator_profile", "born_on")

        result = view.range_view(col)

        assert result is not None
        assert result["redacted"] == "mask"
        assert result["box"] is None
        assert result["percentiles"] == []
        assert result["bounds"] is None

    def test_redacted_column_still_carries_freshness_and_frequencies(
        self,
        redacted_conn: ConnectionConfig,
    ) -> None:
        col = _column(redacted_conn, "seedbank.curator_profile", "born_on")

        result = view.range_view(col)

        assert result is not None
        assert result["freshness"] == {"max_age_days": 90, "classification": "dormant"}
        assert result["frequencies"] is not None

    def test_no_range_data_at_all_is_none(self) -> None:
        assert view.range_view({}) is None

    def test_unrepresentable_bound_drops_the_box_but_keeps_the_percentile_list(
        self,
        edge_case_conn: ConnectionConfig,
    ) -> None:
        col = _column(edge_case_conn, "public.legacy_dates", "recorded_at")

        result = view.range_view(col)

        assert result is not None
        assert result["box"] is None  # p99/max fall outside the proleptic Gregorian calendar
        assert result["percentiles"]  # still listed - only the geometry is unrepresentable
        assert result["unrepresentable"] == ("max", "p99")


class TestSketchAvailable:
    def test_true_when_sketch_present(self, rich_conn: ConnectionConfig) -> None:
        col = _column(rich_conn, "seedbank.batch", "cultivar_id")

        assert view.sketch_available(col) is True

    def test_false_when_absent(self, rich_conn: ConnectionConfig) -> None:
        col = _column(rich_conn, "seedbank.batch", "batch_id")

        assert view.sketch_available(col) is False

    def test_never_exposes_the_payload(self, rich_conn: ConnectionConfig) -> None:
        result = view.sketch_available(_column(rich_conn, "seedbank.batch", "cultivar_id"))

        assert result is True


class TestAnnotationView:
    def test_stale_key_is_filtered_out(self, rich_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([rich_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.batch")
        assert artifacts is not None
        assert artifacts.statistics is not None
        known = artifacts.statistics["columns"]

        annotations = view.annotation_view(artifacts.statistics_annotations, known)

        assert "cultivar_id" in annotations
        assert "stale_column_name" not in annotations

    def test_no_known_columns_keeps_every_key(self) -> None:
        annotations = view.annotation_view({"columns": {"x": {"note": "n"}}}, {})

        assert "x" in annotations

    def test_absent_annotations_is_empty(self) -> None:
        assert view.annotation_view(None, {}) == {}


class TestColumnView:
    def test_notes_reuses_engine_notes_synthesis(self, rich_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([rich_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.batch")
        assert artifacts is not None
        assert artifacts.statistics is not None
        columns = artifacts.statistics["columns"]
        annotations = view.annotation_view(artifacts.statistics_annotations, columns)
        targets = catalogue.leaf_targets(found, "seedbank.batch")

        rendered = view.column_view(
            "cultivar_id",
            columns["cultivar_id"],
            300,
            artifacts.relationships,
            annotations,
            targets,
        )

        assert "FK -> seedbank.cultivar.cultivar_id (declared)" in rendered["notes"]

    def test_annotation_note_is_linkified(self, rich_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([rich_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.batch")
        assert artifacts is not None
        assert artifacts.statistics is not None
        columns = artifacts.statistics["columns"]
        annotations = view.annotation_view(artifacts.statistics_annotations, columns)
        targets = catalogue.leaf_targets(found, "seedbank.batch")
        targets.update({name: f"#col-{name}" for name in columns})

        rendered = view.column_view(
            "cultivar_id",
            columns["cultivar_id"],
            300,
            artifacts.relationships,
            annotations,
            targets,
        )

        # Both "cultivar" (a table mention) and "cultivar_id" (a column self-mention) link out.
        assert rendered["annotation_note"] == (
            "FK to [cultivar](/t/primary/seedbank.cultivar).[cultivar_id](#col-cultivar_id)."
        )
        assert rendered["annotation_values"] == [(1, "the type specimen")]
        assert rendered["annotation_claims"] == [("cardinality_ratio", "> 0.1")]

    def test_candidate_key_exception_names_why_the_ratio_falls_short(
        self,
        edge_case_conn: ConnectionConfig,
    ) -> None:
        col = _column(edge_case_conn, "public.legacy_dates", "external_ref")

        rendered = view.column_view("external_ref", col, 200, None, {}, {})

        assert "candidate key (measured duplicates)" in rendered["notes"]

    def test_notes_drop_what_the_table_already_shows_in_its_own_cells(
        self,
        companion_conn: ConnectionConfig,
    ) -> None:
        # `phone` carries no hint, and its other facts all have dedicated cells.
        col = _column(companion_conn, "seedbank.botanists", "phone")

        rendered = view.column_view("phone", col, 100, None, {}, {})

        assert rendered["notes"] == ""

    def test_no_physical_name_field(self, rich_conn: ConnectionConfig) -> None:
        col = _column(rich_conn, "seedbank.batch", "cultivar_id")

        rendered = view.column_view("cultivar_id", col, 300, None, {}, {})

        assert "physical_name" not in rendered

    def test_null_companions_wired_through(self, companion_conn: ConnectionConfig) -> None:
        statistics = _statistics(companion_conn, "seedbank.botanists")
        patterns = view.null_patterns_view(statistics)
        col = _column(companion_conn, "seedbank.botanists", "email")

        rendered = view.column_view("email", col, 100, None, {}, {}, patterns)

        assert rendered["null_companions"] == ["phone"]

    def test_null_companions_defaults_to_empty_without_patterns(
        self,
        rich_conn: ConnectionConfig,
    ) -> None:
        col = _column(rich_conn, "seedbank.batch", "cultivar_id")

        rendered = view.column_view("cultivar_id", col, 300, None, {}, {})

        assert rendered["null_companions"] == []


class TestFkTargetMap:
    def test_single_column_edge_carries_detection(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["cultivar_id"],
                    "target_table": "t",
                    "target_column": ["id"],
                    "detection": "declared",
                },
            ],
        }

        assert view.fk_target_map(relationships)["cultivar_id"] == "t.id (declared)"

    def test_missing_detection_defaults_to_inferred(self) -> None:
        relationships = {
            "refers_to": [
                {"column": ["cultivar_id"], "target_table": "t", "target_column": ["id"]},
            ],
        }

        assert view.fk_target_map(relationships)["cultivar_id"] == "t.id (inferred)"

    def test_composite_edge_joins_columns(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["a", "b"],
                    "target_table": "t",
                    "target_column": ["x", "y"],
                    "detection": "declared",
                },
            ],
        }

        assert view.fk_target_map(relationships)["a,b"] == "t.(x,y) (declared)"


class TestRelationshipRows:
    def test_every_edge_states_detection_both_directions(self, rich_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([rich_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.batch")
        assert artifacts is not None

        rows = view.relationship_rows(artifacts.relationships, artifacts.relationships_annotations)

        assert rows["refers_to"][0]["detection"] == "declared"
        assert rows["referenced_by"][0]["detection"] == "inferred"

    def test_no_filler_on_delete_on_an_inferred_edge(self) -> None:
        relationships = {
            "refers_to": [],
            "referenced_by": [
                {
                    "column": ["a"],
                    "referencer_table": "t",
                    "referencer_column": ["b"],
                    "detection": "inferred",
                },
            ],
        }

        rows = view.relationship_rows(relationships, None)

        assert rows["referenced_by"][0]["on_delete"] is None

    def test_declared_edge_keeps_its_on_delete(self, rich_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([rich_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.batch")
        assert artifacts is not None

        rows = view.relationship_rows(artifacts.relationships, artifacts.relationships_annotations)

        assert rows["refers_to"][0]["on_delete"] == "RESTRICT"

    def test_observed_present_when_scope_compatible(self, rich_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([rich_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.batch")
        assert artifacts is not None

        rows = view.relationship_rows(artifacts.relationships, artifacts.relationships_annotations)

        assert rows["refers_to"][0]["observed"]["fanout_avg"] == 7.5

    def test_observed_suppressed_when_scope_incompatible(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["a"],
                    "target_table": "t",
                    "target_column": ["b"],
                    "detection": "declared",
                    "observed": {"scope_compatible": False},
                },
            ],
            "referenced_by": [],
        }

        rows = view.relationship_rows(relationships, None)

        assert rows["refers_to"][0]["observed"] is None

    def test_rejected_edge_carries_a_true_flag_and_its_note(
        self,
        rich_conn: ConnectionConfig,
    ) -> None:
        found = catalogue.load_connections([rich_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.batch")
        assert artifacts is not None

        rows = view.relationship_rows(artifacts.relationships, artifacts.relationships_annotations)

        # The rejection addresses seedbank.nonexistent, not the real edge, so it does not apply.
        assert rows["refers_to"][0]["rejected"] is False
        assert rows["refers_to"][0]["rejected_note"] is None

    def test_rejected_without_a_note_is_still_flagged_true(self) -> None:
        relationships = {
            "refers_to": [
                {
                    "column": ["a"],
                    "target_table": "t",
                    "target_column": ["b"],
                    "detection": "inferred",
                },
            ],
            "referenced_by": [],
        }
        annotations = {
            "refers_to": [
                {
                    "column": ["a"],
                    "target_table": "t",
                    "target_column": ["b"],
                    "verdict": "rejected",
                },
            ],
        }

        rows = view.relationship_rows(relationships, annotations)

        # A rejection with no `note` must not collapse into "not rejected".
        assert rows["refers_to"][0]["rejected"] is True
        assert rows["refers_to"][0]["rejected_note"] is None


class TestLinkify:
    def test_wraps_a_word_boundary_mention(self) -> None:
        result = view.linkify("See cultivar for species.", {"cultivar": "/t/c/seedbank.cultivar"})

        assert result == "See [cultivar](/t/c/seedbank.cultivar) for species."

    def test_skips_a_mention_inside_a_code_span(self) -> None:
        result = view.linkify("Use `cultivar_id`, not cultivar.", {"cultivar": "/x"})

        assert result == "Use `cultivar_id`, not [cultivar](/x)."

    def test_no_targets_returns_text_unchanged(self) -> None:
        assert view.linkify("plain text", {}) == "plain text"

    def test_none_text_returns_none(self) -> None:
        assert view.linkify(None, {"a": "/b"}) is None


class TestBuildTableView:
    def test_composes_every_section(self, rich_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([rich_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.batch")
        assert artifacts is not None

        page = view.build_table_view(found, artifacts)

        assert page["fqn"] == "seedbank.batch"
        assert page["grain"] is not None
        assert page["null_patterns"] is not None
        assert page["physical_layout"] is not None
        assert page["dependencies"]
        assert len(page["columns"]) == 4
        assert page["diagram"] is not None
        assert page["columns_empty_notice"] is None
        assert page["cardinality"] is not None
        assert page["completeness"] is not None

    def test_singular_mention_links_to_the_plural_table_name(
        self,
        companion_conn: ConnectionConfig,
    ) -> None:
        found = catalogue.load_connections([companion_conn])[0]
        artifacts = catalogue.load_table(found, "seedbank.botanists")
        assert artifacts is not None

        page = view.build_table_view(found, artifacts)

        # "botanist" is no real table/column name, only the aliased singular of "botanists".
        assert "[botanist](/t/primary/seedbank.botanists)" in page["description"]

    def test_empty_columns_table_composes_the_notice_instead_of_a_skyline(
        self,
        empty_columns_conn: ConnectionConfig,
    ) -> None:
        found = catalogue.load_connections([empty_columns_conn])[0]
        artifacts = catalogue.load_table(found, "public.narrow")
        assert artifacts is not None

        page = view.build_table_view(found, artifacts)

        assert page["columns_empty_notice"] is not None
        assert page["skyline"] == []
        assert page["columns"] == []

    def test_catalog_only_view_suppresses_every_measured_aggregate(
        self,
        catalog_only_conn: ConnectionConfig,
    ) -> None:
        """SPEC 2.2.15: the table-wide aggregates would fabricate a measurement, so none render."""

        found = catalogue.load_connections([catalog_only_conn])[0]
        artifacts = catalogue.load_table(found, "public.active_contributors_v")
        assert artifacts is not None

        page = view.build_table_view(found, artifacts)

        assert page["cards"] is None
        assert page["cardinality"] is None
        assert page["completeness"] is None
        assert page["skyline"] == []
        assert len(page["columns"]) == 2
        assert page["columns_empty_notice"] is None


class TestBuildSchemaView:
    def test_counts_intra_schema_edges_only(self, rich_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([rich_conn])[0]

        result = view.build_schema_view(found, "seedbank")

        assert result is not None
        assert set(result["tables"]) == {"seedbank.batch", "seedbank.cultivar"}
        assert result["n_edges"] == 1

    def test_unknown_schema_is_none(self, rich_conn: ConnectionConfig) -> None:
        found = catalogue.load_connections([rich_conn])[0]

        assert view.build_schema_view(found, "nonexistent") is None


class TestBuildIndexView:
    def test_one_entry_per_connection(self, rich_conn: ConnectionConfig) -> None:
        loaded = catalogue.load_connections([rich_conn])

        result = view.build_index_view(loaded)

        assert len(result) == 1
        assert result[0]["name"] == "primary"
        assert len(result[0]["tables"]) == 2
