"""diff.yaml invariants beyond its JSON Schema, per SPEC 2.6."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.conformance import diff
from dbprint.conformance.issue import Issue


_PATH = "prints/c/diff.yaml"
_ZERO_SUMMARY = {
    "tables_added": 0,
    "tables_removed": 0,
    "tables_modified": 0,
    "columns_added": 0,
    "columns_removed": 0,
    "columns_type_changed": 0,
    "columns_nullable_changed": 0,
    "columns_default_changed": 0,
    "statistics_drifted": 0,
    "relationships_changed": 0,
    "indexes_changed": 0,
    "comments_changed": 0,
    "unchanged_tables": 0,
    "unevaluated_tables": 0,
}


def _body(changes: list[Any], **summary: int) -> dict[str, Any]:
    return {"changes": changes, "summary": {**_ZERO_SUMMARY, **summary}}


def _one_change(change: dict[str, Any], **summary: int) -> list[Issue]:
    return diff.check(_body([change], **summary), _PATH)


def _error(code: str, detail: str, index: int = 0) -> Issue:
    return Issue(f"{_PATH}::changes[{index}]", code, "error", detail, "§2.6.6")


class TestTheSummaryCountsTheEvents:
    def test_a_body_that_is_not_a_mapping_is_left_to_the_schema(self) -> None:
        assert diff.check(["changes"], _PATH) == []

    def test_counts_that_agree_raise_nothing(self) -> None:
        changes = [
            {"kind": "column_added", "table": "s.t", "column": "a"},
            {"kind": "column_added", "table": "s.t", "column": "b"},
            {"kind": "relationship_added"},
            {"kind": "relationship_removed"},
            {"kind": "index_added"},
            {"kind": "comment_changed", "target": "table"},
        ]

        assert (
            diff.check(
                _body(
                    changes,
                    columns_added=2,
                    relationships_changed=2,
                    indexes_changed=1,
                    comments_changed=1,
                ),
                _PATH,
            )
            == []
        )

    def test_every_disagreeing_counter_is_named_in_one_warning(self) -> None:
        changes = [
            "not an event",
            {"kind": 7},
            {"kind": "table_added", "table": "s.a"},
            {"kind": "statistic_changed", "stat": "cardinality", "delta": 1, "delta_pct": 0.1},
            {"kind": "relationship_modified", "on_delete": "cascade"},
        ]

        assert diff.check(_body(changes, tables_added=2, indexes_changed=3), _PATH) == [
            Issue(
                _PATH,
                "diff.summary-count-mismatch",
                "warning",
                "tables_added reports 2, actual events for table_added: 1; "
                "statistics_drifted reports 0, actual events for statistic_changed: 1; "
                "relationships_changed reports 0, actual events sum: 1; "
                "indexes_changed reports 3, actual events sum: 0",
                "§2.6.4",
            ),
        ]

    def test_an_absent_counter_reads_as_zero(self) -> None:
        assert diff.check({"changes": []}, _PATH) == []

    def test_an_absent_counter_with_events_names_none(self) -> None:
        assert diff.check({"changes": [{"kind": "index_removed"}]}, _PATH) == [
            Issue(
                _PATH,
                "diff.summary-count-mismatch",
                "warning",
                "indexes_changed reports None, actual events sum: 1",
                "§2.6.4",
            ),
        ]


class TestTheTableCountersPartitionTheScannedTables:
    def _body(self, scanned: Any, **counters: Any) -> dict[str, Any]:
        summary = {
            **_ZERO_SUMMARY,
            "tables_modified": 1,
            "unchanged_tables": 2,
            "unevaluated_tables": 3,
            "tables_added": 4,
            **counters,
        }

        return {
            "changes": [{"kind": "table_added"}] * summary["tables_added"],
            "summary": summary,
            "target": {"tables_scanned": scanned},
        }

    def test_counters_that_sum_to_the_scanned_count_pass(self) -> None:
        assert diff.check(self._body(10), _PATH) == []

    def test_counters_that_do_not_are_named_with_their_sum(self) -> None:
        assert diff.check(self._body(9), _PATH) == [
            Issue(
                _PATH,
                "diff.summary-total-mismatch",
                "warning",
                "tables_modified=1, unchanged_tables=2, unevaluated_tables=3, tables_added=4 "
                "sum to 10, but target.tables_scanned is 9.",
                "§2.6.4",
            ),
        ]

    @pytest.mark.parametrize("scanned", [None, -1, True, 9.0, "9"])
    def test_a_scanned_count_that_is_not_a_count_is_left_to_the_schema(self, scanned: Any) -> None:
        assert diff.check(self._body(scanned), _PATH) == []

    @pytest.mark.parametrize("operand", [None, -1, False, 2.0])
    def test_a_counter_that_is_not_a_count_is_left_to_the_schema(self, operand: Any) -> None:
        assert diff.check(self._body(9, unchanged_tables=operand), _PATH) == []

    def test_a_target_that_is_not_a_mapping_is_left_to_the_schema(self) -> None:
        body = self._body(9)
        body["target"] = ["tables_scanned", 9]

        assert diff.check(body, _PATH) == []


class TestEachEventKindCarriesWhatItsKindRequires:
    def test_a_relationship_modified_event_names_what_changed(self) -> None:
        assert _one_change({"kind": "relationship_modified"}, relationships_changed=1) == [
            _error(
                "diff.relationship-modified-no-change",
                "relationship_modified event must carry on_delete and/or on_update.",
            ),
        ]

    @pytest.mark.parametrize("field", ["on_delete", "on_update"])
    def test_either_referential_action_is_enough(self, field: str) -> None:
        change = {"kind": "relationship_modified", field: "cascade"}

        assert _one_change(change, relationships_changed=1) == []

    def test_a_column_comment_names_its_column(self) -> None:
        assert _one_change({"kind": "comment_changed", "target": "column"}, comments_changed=1) == [
            _error(
                "diff.comment-target-column-mismatch",
                "comment_changed with target=column requires the `column` field.",
            ),
        ]

    def test_a_table_comment_names_no_column(self) -> None:
        change = {"kind": "comment_changed", "target": "table", "column": "a"}

        assert _one_change(change, comments_changed=1) == [
            _error(
                "diff.comment-target-column-mismatch",
                "comment_changed with target=table MUST NOT carry the `column` field.",
            ),
        ]

    @pytest.mark.parametrize(
        "change",
        [
            {"kind": "comment_changed", "target": "column", "column": "a"},
            {"kind": "comment_changed", "target": "table"},
        ],
    )
    def test_a_well_targeted_comment_passes(self, change: dict[str, Any]) -> None:
        assert _one_change(change, comments_changed=1) == []

    def test_a_statistic_change_names_a_measured_statistic(self) -> None:
        change = {"kind": "statistic_changed", "stat": "sql_type"}

        assert _one_change(change, statistics_drifted=1) == [
            _error(
                "diff.statistic-changed-not-a-measurement",
                "statistic_changed names 'sql_type', which is not a measured statistic "
                "(SPEC 2.6.6).",
            ),
        ]

    def test_a_statistic_change_with_no_stat_names_the_empty_string(self) -> None:
        assert _one_change({"kind": "statistic_changed"}, statistics_drifted=1) == [
            _error(
                "diff.statistic-changed-not-a-measurement",
                "statistic_changed names '', which is not a measured statistic (SPEC 2.6.6).",
            ),
        ]

    def test_a_stat_that_is_not_text_is_left_to_the_schema(self) -> None:
        assert _one_change({"kind": "statistic_changed", "stat": 3}, statistics_drifted=1) == []

    @pytest.mark.parametrize("stat", ["values", "distribution", "classification"])
    @pytest.mark.parametrize("delta", [{"delta": 1}, {"delta_pct": 0.5}])
    def test_a_non_numeric_statistic_carries_no_delta(
        self,
        stat: str,
        delta: dict[str, Any],
    ) -> None:
        change = {"kind": "statistic_changed", "stat": stat, **delta}

        assert _one_change(change, statistics_drifted=1) == [
            _error(
                "diff.statistic-changed-delta-on-non-numeric",
                f"statistic_changed for {stat!r} (non-numeric) MUST NOT carry delta / delta_pct.",
            ),
        ]

    def test_a_non_numeric_statistic_without_a_delta_passes(self) -> None:
        assert (
            _one_change({"kind": "statistic_changed", "stat": "values"}, statistics_drifted=1) == []
        )

    @pytest.mark.parametrize(
        ("delta", "delta_pct"),
        [(5, -0.1), (-5, 0.1), (0, 0.1), (5, 0), (0.0, -0.1)],
    )
    def test_delta_and_its_percentage_agree_in_sign(self, delta: float, delta_pct: float) -> None:
        change = {
            "kind": "statistic_changed",
            "stat": "cardinality",
            "delta": delta,
            "delta_pct": delta_pct,
        }

        assert _one_change(change, statistics_drifted=1) == [
            _error(
                "diff.statistic-changed-delta-pct-sign-mismatch",
                f"delta={delta!r} and delta_pct={delta_pct!r} disagree in sign.",
            ),
        ]

    @pytest.mark.parametrize(
        ("delta", "delta_pct"),
        [(5, 0.1), (-5, -0.1), (0, 0), (0.0, 0.0), (5, None), (None, 0.1), ("5", 0.1)],
    )
    def test_agreeing_or_absent_signs_pass(self, delta: Any, delta_pct: Any) -> None:
        change = {
            "kind": "statistic_changed",
            "stat": "cardinality",
            "delta": delta,
            "delta_pct": delta_pct,
        }

        assert _one_change(change, statistics_drifted=1) == []

    def test_a_row_count_delta_equals_after_minus_before(self) -> None:
        change = {"kind": "table_row_count_changed", "before": 10, "after": 15, "delta": 4}

        assert _one_change(change) == [
            _error(
                "diff.row-count-changed-delta-mismatch",
                "delta=4 does not equal after (15) - before (10).",
            ),
        ]

    @pytest.mark.parametrize(
        "change",
        [
            {"before": 10, "after": 15, "delta": 5},
            {"before": 15, "after": 10, "delta": -5},
            {"before": 10.0, "after": 15, "delta": 4},
            {"before": 10, "after": "15", "delta": 4},
            {"before": 10, "after": 15, "delta": None},
        ],
    )
    def test_a_matching_or_untyped_row_count_passes(self, change: dict[str, Any]) -> None:
        assert _one_change({"kind": "table_row_count_changed", **change}) == []

    @pytest.mark.parametrize(
        ("kind", "code"),
        [
            ("grain_changed", "diff.grain-changed-no-change"),
            ("physical_layout_changed", "diff.physical-layout-changed-no-change"),
            ("depends_on_changed", "diff.depends-on-changed-no-change"),
            ("table_type_changed", "diff.table-type-changed-no-change"),
            ("column_physical_name_changed", "diff.column-physical-name-changed-no-change"),
            ("column_collation_changed", "diff.column-collation-changed-no-change"),
        ],
    )
    def test_a_change_event_whose_sides_are_identical(self, kind: str, code: str) -> None:
        assert _one_change({"kind": kind, "before": "x", "after": "x"}) == [
            _error(code, f"{kind} event's before and after are identical."),
        ]

    @pytest.mark.parametrize(
        "kind",
        [
            "grain_changed",
            "physical_layout_changed",
            "depends_on_changed",
            "table_type_changed",
            "column_physical_name_changed",
            "column_collation_changed",
        ],
    )
    def test_a_change_event_whose_sides_differ_passes(self, kind: str) -> None:
        assert _one_change({"kind": kind, "before": "x", "after": "y"}) == []

    def test_each_event_is_addressed_by_its_own_index(self) -> None:
        changes = [
            {"kind": "table_type_changed", "before": "view", "after": "table"},
            "not an event",
            {"kind": "grain_changed", "before": [], "after": []},
            {"kind": "table_type_changed", "before": "view", "after": "view"},
        ]

        assert diff.check(_body(changes), _PATH) == [
            _error(
                "diff.grain-changed-no-change",
                "grain_changed event's before and after are identical.",
                2,
            ),
            _error(
                "diff.table-type-changed-no-change",
                "table_type_changed event's before and after are identical.",
                3,
            ),
        ]


def _print_with(tmp_path: Path, columns: dict[str, Any], **entry: Any) -> dict[str, Any]:
    table_dir = tmp_path / "s" / "t"
    table_dir.mkdir(parents=True)
    (table_dir / "statistics.yaml").write_text(yaml.safe_dump({"columns": columns}))

    return {
        "tables": {"s.t": {"path": "s/t", "artifacts": {"statistics": "statistics.yaml"}, **entry}},
    }


class TestARedactedColumnIsNeverComparedOnItsValues:
    def _changes(self, *stats: str, column: str = "email") -> dict[str, Any]:
        return {
            "changes": [
                {"kind": "statistic_changed", "table": "s.t", "column": column, "stat": stat}
                for stat in stats
            ],
        }

    def test_a_value_bearing_stat_compared_on_a_redacted_column(self, tmp_path: Path) -> None:
        manifest = _print_with(tmp_path, {"email": {"redacted": "mask"}, "id": {}})

        assert diff.check_redacted_values(
            tmp_path,
            manifest,
            self._changes("cardinality", "range.max"),
            _PATH,
        ) == [
            Issue(
                f"{_PATH}::changes[1]",
                "privacy.redacted-value-compared",
                "error",
                "stat 'range.max' is compared on 'email', whose statistics declare a `redacted` "
                "marker over that value.",
                "§2.6.6",
            ),
        ]

    def test_an_unredacted_column_may_compare_its_values(self, tmp_path: Path) -> None:
        manifest = _print_with(tmp_path, {"email": {"redacted": "mask"}, "id": {}})

        assert (
            diff.check_redacted_values(
                tmp_path,
                manifest,
                self._changes("range.max", column="id"),
                _PATH,
            )
            == []
        )

    def test_only_statistic_events_are_read(self, tmp_path: Path) -> None:
        manifest = _print_with(tmp_path, {"email": {"redacted": "mask"}})
        body = {
            "changes": [
                {
                    "kind": "column_type_changed",
                    "table": "s.t",
                    "column": "email",
                    "stat": "values",
                },
                "not an event",
                {"kind": "statistic_changed", "table": "s.t", "column": "email", "stat": 5},
                {"kind": "statistic_changed", "table": "s.t", "column": "email", "stat": "values"},
            ],
        }

        assert [i.path for i in diff.check_redacted_values(tmp_path, manifest, body, _PATH)] == [
            f"{_PATH}::changes[3]",
        ]

    def test_a_print_with_no_redacted_column_reads_no_event(self, tmp_path: Path) -> None:
        manifest = _print_with(tmp_path, {"email": {"redacted": None}})

        assert diff.check_redacted_values(tmp_path, manifest, self._changes("values"), _PATH) == []

    def test_a_body_that_is_not_a_mapping_is_left_to_the_schema(self, tmp_path: Path) -> None:
        manifest = _print_with(tmp_path, {"email": {"redacted": "mask"}})

        assert diff.check_redacted_values(tmp_path, manifest, ["changes"], _PATH) == []

    @pytest.mark.parametrize(
        "statistics",
        ["columns: [unclosed", "- a list", "columns: {email: not-a-mapping}"],
    )
    def test_statistics_no_reader_can_use_mark_nothing(
        self,
        tmp_path: Path,
        statistics: str,
    ) -> None:
        manifest = _print_with(tmp_path, {})
        (tmp_path / "s" / "t" / "statistics.yaml").write_text(statistics)

        assert diff.check_redacted_values(tmp_path, manifest, self._changes("values"), _PATH) == []

    def test_a_table_whose_statistics_file_is_missing_or_undeclared_marks_nothing(
        self,
        tmp_path: Path,
    ) -> None:
        manifest = _print_with(tmp_path, {"email": {"redacted": "mask"}})
        manifest["tables"]["s.u"] = {"path": "s/u", "artifacts": {"statistics": "statistics.yaml"}}
        manifest["tables"]["s.t"]["artifacts"] = {"ddl": "ddl.sql"}

        assert diff.check_redacted_values(tmp_path, manifest, self._changes("values"), _PATH) == []


class TestEveryTableAndEventIsRead:
    def test_an_event_on_an_unredacted_column_does_not_hide_a_later_one(
        self,
        tmp_path: Path,
    ) -> None:
        manifest = _print_with(tmp_path, {"email": {"redacted": "hash"}, "id": {}})
        body = {
            "changes": [
                {"kind": "statistic_changed", "table": "s.t", "column": "id", "stat": "values"},
                {"kind": "statistic_changed", "table": "s.t", "column": "email", "stat": "values"},
            ],
        }

        assert [i.path for i in diff.check_redacted_values(tmp_path, manifest, body, _PATH)] == [
            f"{_PATH}::changes[1]",
        ]

    def test_tables_no_reader_can_use_do_not_hide_a_later_redacted_one(
        self,
        tmp_path: Path,
    ) -> None:
        for name, text in (
            ("c", "columns: [unclosed"),
            ("d", "- a list"),
            ("e", "columns: {email: {redacted: mask}}"),
        ):
            (tmp_path / "s" / name).mkdir(parents=True)
            (tmp_path / "s" / name / "statistics.yaml").write_text(text)

        stats = {"statistics": "statistics.yaml"}
        manifest = {
            "tables": {
                "s.a": {"path": "s/a", "artifacts": {"ddl": "ddl.sql"}},
                "s.b": {"path": "s/b", "artifacts": stats},
                "s.c": {"path": "s/c", "artifacts": stats},
                "s.d": {"path": "s/d", "artifacts": stats},
                "s.e": {"path": "s/e", "artifacts": stats},
            },
        }
        body = {
            "changes": [
                {"kind": "statistic_changed", "table": "s.e", "column": "email", "stat": "values"},
            ],
        }

        assert len(diff.check_redacted_values(tmp_path, manifest, body, _PATH)) == 1

    def test_an_entry_with_no_path_reads_its_statistics_at_the_print_root(
        self,
        tmp_path: Path,
    ) -> None:
        (tmp_path / "statistics.yaml").write_text("columns: {email: {redacted: mask}}")
        manifest = {"tables": {"s.t": {"artifacts": {"statistics": "statistics.yaml"}}}}
        body = {
            "changes": [
                {"kind": "statistic_changed", "table": "s.t", "column": "email", "stat": "values"},
            ],
        }

        assert len(diff.check_redacted_values(tmp_path, manifest, body, _PATH)) == 1
