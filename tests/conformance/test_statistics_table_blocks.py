"""Table-grain blocks of statistics.yaml - null patterns, layout, grain, dependencies, timeline."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

import pytest

from dbprint.conformance import statistics as st
from dbprint.conformance.issue import Issue


_P = "p.yaml"
_Severity = Literal["error", "warning"]


def _issue(
    code: str,
    detail: str,
    spec_ref: str,
    severity: _Severity = "error",
    path: str = _P,
) -> Issue:
    return Issue(path, code, severity, detail, spec_ref)


class TestNullPatternsPresence:
    def test_nulls_with_no_block(self) -> None:
        columns = {"a": {"null_count": 0}, "b": "x", "c": {"null_count": 2}}

        assert st._check_null_patterns({}, _P, columns, 10) == [
            _issue(
                "stats.null-patterns-absent-with-nulls",
                "a column carries nulls but no `null_patterns` block says which columns carry "
                "them together; an absent block asserts the table has no nulls",
                "§2.2.10",
            ),
        ]

    def test_a_block_that_is_not_a_mapping_reads_as_absent(self) -> None:
        assert [
            i.code
            for i in st._check_null_patterns(
                {"null_patterns": []},
                _P,
                {"c": {"null_count": 2}},
                10,
            )
        ] == [
            "stats.null-patterns-absent-with-nulls",
        ]

    def test_nulls_with_the_block_named_unmeasured(self) -> None:
        data = {"unmeasured": ["null_patterns"]}

        assert st._check_null_patterns(data, _P, {"c": {"null_count": 2}}, 10) == []

    def test_no_nulls_and_no_block(self) -> None:
        assert (
            st._check_null_patterns({}, _P, {"c": {"null_count": 0}, "d": {"null_count": "2"}}, 10)
            == []
        )

    def test_a_block_with_no_nulls(self) -> None:
        data = {"null_patterns": {"patterns": "x"}}

        assert st._check_null_patterns(data, _P, {"c": {"null_count": 0}}, 10) == [
            _issue(
                "stats.null-patterns-absent-with-nulls",
                "a `null_patterns` block is present but no column reports a null; the block is "
                "emitted only when some column carries one",
                "§2.2.10",
            ),
        ]


def _patterns(*entries: Any, **block: Any) -> dict[str, Any]:
    return {"null_patterns": {"patterns": list(entries), **block}}


class TestNullPatternEntries:
    _COLUMNS: ClassVar[dict[str, Any]] = {"a": {"null_count": 3}, "b": {"null_count": 1}}

    def test_a_consistent_census_passes(self) -> None:
        data = _patterns(
            {"columns": ["a"], "count": 2},
            {"columns": ["a", "b"], "count": 1},
            "x",
            coverage=0.3,
        )

        assert st._check_null_patterns(data, _P, self._COLUMNS, 10) == []

    def test_an_unknown_column(self) -> None:
        data = _patterns({"columns": ["a", "zz", 5], "count": 1})

        assert _issue(
            "stats.null-patterns-unknown-column",
            "`null_patterns` names ['zz'], absent from this file's `columns` map",
            "§2.2.10",
        ) in st._check_null_patterns(data, _P, self._COLUMNS, 10)

    def test_a_repeated_combination(self) -> None:
        data = _patterns({"columns": ["a"], "count": 1}, {"columns": ["a"], "count": 1})

        assert _issue(
            "stats.null-patterns-duplicate-combination",
            "`null_patterns` lists [['a']] more than once; entries partition the rows they "
            "cover, so each combination appears at most once",
            "§2.2.10",
        ) in st._check_null_patterns(data, _P, self._COLUMNS, 10)

    def test_entries_out_of_order(self) -> None:
        data = _patterns({"columns": ["b"], "count": 1}, {"columns": ["a"], "count": 1})

        assert _issue(
            "stats.null-patterns-not-ordered",
            "`null_patterns.patterns` is not ordered by `count` descending with ties broken by "
            "ascending `columns`",
            "§2.2.10",
        ) in st._check_null_patterns(data, _P, self._COLUMNS, 10)

    @pytest.mark.parametrize(
        ("method", "code", "severity"),
        [
            ("bounded", "stats.null-patterns-sum-exceeds-rows-scanned-bounded", "warning"),
            ("measured", "stats.null-patterns-sum-exceeds-rows-scanned", "error"),
        ],
    )
    def test_counts_beyond_the_rows_scanned(
        self,
        method: str,
        code: str,
        severity: _Severity,
    ) -> None:
        data = _patterns({"columns": ["a"], "count": 3}, coverage_method=method)

        assert _issue(
            code,
            "`null_patterns` counts sum to 3 over 2 rows scanned; the entries partition the rows "
            "they cover, so they cannot exceed them",
            "§2.2.10",
            severity,
        ) in st._check_null_patterns(data, _P, self._COLUMNS, 2)

    def test_a_coverage_the_counts_do_not_give(self) -> None:
        data = _patterns({"columns": ["a"], "count": 3}, coverage=0.5)

        assert _issue(
            "stats.null-patterns-coverage-mismatch",
            "`null_patterns.coverage` is 0.5 but the listed counts (3) over the rows scanned (10) "
            "give 0.3",
            "§2.2.10",
        ) in st._check_null_patterns(data, _P, self._COLUMNS, 10)

    @pytest.mark.parametrize("coverage", [True, "0.3", None])
    def test_a_coverage_that_is_not_a_number_is_left_to_the_schema(self, coverage: Any) -> None:
        data = _patterns({"columns": ["a"], "count": 3}, coverage=coverage)

        assert st._check_null_patterns(data, _P, self._COLUMNS, 10) == []

    def test_a_coverage_within_the_rounding_tolerance_passes(self) -> None:
        data = _patterns(
            {"columns": ["a"], "count": 1},
            {"columns": ["b"], "count": 1},
            coverage=0.3333339,
        )

        assert (
            st._check_null_patterns(data, _P, {"a": {"null_count": 1}, "b": {"null_count": 1}}, 6)
            == []
        )

    @pytest.mark.parametrize(
        ("method", "code", "severity"),
        [
            ("bounded", "stats.null-patterns-reconciliation-mismatch-bounded", "warning"),
            (None, "stats.null-patterns-reconciliation-mismatch", "error"),
        ],
    )
    def test_patterns_naming_more_nulls_than_the_column_reports(
        self,
        method: str | None,
        code: str,
        severity: _Severity,
    ) -> None:
        data = _patterns({"columns": ["b"], "count": 2}, coverage_method=method)

        assert _issue(
            code,
            "the `null_patterns` entries naming 'b' account for 2 null rows, but the column "
            "reports null_count 1",
            "§2.2.10",
            severity,
        ) in st._check_null_patterns(data, _P, {"b": {"null_count": 1}}, 10)

    def test_a_complete_census_must_account_for_every_null(self) -> None:
        data = _patterns({"columns": ["a"], "count": 2}, coverage=1.0)
        columns = {"z": "x", "y": {"null_count": "1"}, "a": {"null_count": 3}}

        issues = st._check_null_patterns(data, _P, columns, 2)

        assert [i.code for i in issues if "reconciliation" in i.code] == [
            "stats.null-patterns-reconciliation-mismatch",
        ]

    @pytest.mark.parametrize("coverage", [True, 0.9, "1.0"])
    def test_an_incomplete_census_may_account_for_fewer(self, coverage: Any) -> None:
        data = _patterns({"columns": ["a"], "count": 2}, coverage=coverage)

        issues = st._check_null_patterns(data, _P, {"a": {"null_count": 3}}, 2)

        assert [i.code for i in issues if "reconciliation" in i.code] == []

    @pytest.mark.parametrize("count", [True, "2", None])
    def test_a_count_that_is_not_an_integer_counts_as_zero(self, count: Any) -> None:
        data = _patterns({"columns": ["a"], "count": count}, coverage=0.0)

        assert st._check_null_patterns(data, _P, {"a": {"null_count": 1}}, 10) == []


class TestDependsOn:
    def test_a_table_carrying_it(self) -> None:
        assert st._check_depends_on({"type": "table", "depends_on": []}, _P) == [
            _issue(
                "stats.depends-on-on-table",
                "depends_on is present but type is 'table'; the field names what a view/matview "
                "reads and MUST NOT appear on a plain table.",
                "§2.2.17",
            ),
        ]

    @pytest.mark.parametrize("data", [{"type": "view", "depends_on": []}, {"type": "table"}])
    def test_a_view_or_an_absent_field_passes(self, data: dict[str, Any]) -> None:
        assert st._check_depends_on(data, _P) == []


class TestPhysicalLayout:
    def test_keys_and_markers_that_disagree(self) -> None:
        data = {
            "physical_layout": {
                "keys": ["x", {"column": "a"}, {"column": "zz"}, {"expression": "f(b)"}],
            },
        }
        columns = {"a": {}, "b": {"physical_layout_key": True}, "c": "x"}

        assert st._check_physical_layout(data, _P, columns) == [
            _issue(
                "stats.physical-layout-unknown-column",
                "physical_layout.keys names column(s) ['zz'] not present in `columns`.",
                "§2.2.11",
            ),
            _issue(
                "stats.physical-layout-key-not-declared",
                "column(s) ['b'] carry `physical_layout_key: true` but are not named in "
                "physical_layout.keys.",
                "§2.2.11",
            ),
            _issue(
                "stats.physical-layout-key-missing-marker",
                "physical_layout.keys names column(s) ['a', 'zz'] that do not carry "
                "`physical_layout_key: true`.",
                "§2.2.11",
            ),
        ]

    def test_agreeing_keys_and_markers_pass(self) -> None:
        data = {"physical_layout": {"keys": [{"column": "a"}]}}

        assert st._check_physical_layout(data, _P, {"a": {"physical_layout_key": True}}) == []


class TestGrain:
    def test_every_violation_is_named(self) -> None:
        keys = [
            "x",
            {"columns": "a"},
            {"columns": ["b", "a", 3]},
            {"columns": ["a", "b"], "detection": "measured"},
            {"columns": ["zz"]},
        ]
        data = {"grain": {"keys": keys}}

        assert st._check_grain(data, _P, {"a": {}, "b": {}}, 0, True) == [
            _issue(
                "stats.grain-unknown-column",
                "grain.keys names column(s) ['zz'] not present in `columns`.",
                "§2.2.12",
            ),
            _issue(
                "stats.grain-duplicate-key",
                "grain.keys lists [['a', 'b']] more than once.",
                "§2.2.12",
            ),
            _issue(
                "stats.grain-measured-under-scope",
                "grain.keys carries a `measured` entry on a file that also carries `scope`; "
                "uniqueness over a sample is not uniqueness.",
                "§2.2.12",
            ),
            _issue(
                "stats.grain-measured-on-empty-table",
                "grain.keys carries a `measured` entry on a table with row_count 0; every "
                "combination is trivially unique there and MUST be excluded.",
                "§2.2.12",
            ),
        ]

    def test_a_declared_key_on_a_scoped_empty_table_passes(self) -> None:
        data = {"grain": {"keys": [{"columns": ["a"], "detection": "declared"}]}}

        assert st._check_grain(data, _P, {"a": {}}, 0, True) == []


class TestDependencies:
    _COLUMNS: ClassVar[dict[str, Any]] = {
        "a": {"cardinality": 5},
        "b": {"cardinality": 9},
        "c": "x",
    }

    def test_every_violation_is_counted_and_named(self) -> None:
        entries = [
            "x",
            {"determinant": "a", "dependent": "b", "strength": 1.0},
            {"determinant": "a", "dependent": "b", "strength": 0.5},
            {"determinant": "a", "dependent": "a", "strength": 0},
            {"determinant": "b", "dependent": "b", "strength": 2},
            {"determinant": "zz", "dependent": "yy", "strength": "1"},
            {"determinant": 3, "dependent": 4, "strength": 1},
        ]

        assert st._check_dependencies({"dependencies": entries}, _P, self._COLUMNS, 0, True) == [
            _issue(
                "stats.dependencies-unknown-column",
                "dependencies names column(s) ['yy', 'zz'] not present in `columns`.",
                "§2.2.13",
            ),
            _issue(
                "stats.dependencies-self-referential",
                "dependencies carries 2 entry(ies) whose determinant and dependent name the same "
                "column.",
                "§2.2.13",
            ),
            _issue(
                "stats.dependencies-strength-out-of-range",
                "dependencies carries 3 entry(ies) whose strength is not in (0, 1].",
                "§2.2.13",
            ),
            _issue(
                "stats.dependencies-direction-impossible",
                "dependencies carries 2 entry(ies) whose determinant has lower cardinality than "
                "its dependent - impossible for that direction.",
                "§2.2.13",
            ),
            _issue(
                "stats.dependencies-measured-under-scope",
                "dependencies is non-empty on a file that also carries `scope`; a dependency "
                "measured over a sample is not a dependency.",
                "§2.2.13",
            ),
            _issue(
                "stats.dependencies-measured-on-empty-table",
                "dependencies is non-empty on a table with row_count 0; every combination is "
                "trivially functional there and MUST be excluded.",
                "§2.2.13",
            ),
        ]

    def test_a_possible_direction_with_a_valid_strength_passes(self) -> None:
        entries = [{"determinant": "b", "dependent": "a", "strength": 1}]

        assert st._check_dependencies({"dependencies": entries}, _P, self._COLUMNS, 10, False) == []

    def test_a_column_whose_cardinality_is_unknown_decides_no_direction(self) -> None:
        entries = [
            {"determinant": "c", "dependent": "b", "strength": 1},
            {"determinant": "a", "dependent": "c", "strength": 1},
        ]

        assert st._check_dependencies({"dependencies": entries}, _P, self._COLUMNS, 10, False) == []


class TestTimeline:
    _COLUMNS: ClassVar[dict[str, Any]] = {
        "at": {"classification": "temporal"},
        "n": {"classification": "numeric", "redacted": "mask"},
    }

    def _check(
        self,
        block: dict[str, Any],
        *,
        row_count: int = 10,
        scoped: bool = False,
        rows: int = 10,
    ) -> list[Issue]:
        return st._check_timeline({"timeline": block}, _P, self._COLUMNS, row_count, scoped, rows)

    def test_a_consistent_timeline_passes(self) -> None:
        buckets = [{"start": "2026-01-01", "count": 4}, "x", {"start": "2026-01-02", "count": 6}]

        assert self._check({"column": "at", "buckets": buckets, "coverage": 1.0}) == []

    @pytest.mark.parametrize("column", ["zz", 5])
    def test_an_unknown_anchor(self, column: Any) -> None:
        assert self._check({"column": column}) == [
            _issue(
                "stats.timeline-unknown-column",
                f"timeline.column names {column!r}, not present in `columns`.",
                "§2.2.16",
            ),
        ]

    def test_an_anchor_that_is_not_temporal_and_is_redacted(self) -> None:
        assert self._check({"column": "n"}) == [
            _issue(
                "stats.timeline-anchor-not-temporal",
                "timeline.column 'n' classifies 'numeric', not `temporal`.",
                "§2.2.16",
            ),
            _issue(
                "stats.timeline-anchor-redacted",
                "timeline.column 'n' carries a `redacted` marker; the anchor rule MUST never "
                "choose a redacted column.",
                "§2.2.16",
            ),
        ]

    def test_a_timeline_under_scope_on_an_empty_table(self) -> None:
        assert self._check({"column": "at"}, row_count=0, scoped=True) == [
            _issue(
                "stats.timeline-under-scope",
                "timeline is present on a file that also carries `scope`; a bucketed count over "
                "a sample is not a timeline.",
                "§2.2.16",
            ),
            _issue(
                "stats.timeline-on-empty-table",
                "timeline is present on a table with row_count 0; there is nothing to bucket.",
                "§2.2.16",
            ),
        ]

    def test_buckets_out_of_order(self) -> None:
        buckets = [{"start": "2026-01-02", "count": 1}, {"start": "2026-01-01", "count": 1}]

        assert self._check({"column": "at", "buckets": buckets}) == [
            _issue(
                "stats.timeline-buckets-unordered",
                "timeline.buckets is not ascending by `start`.",
                "§2.2.16",
            ),
        ]

    def test_bucket_counts_beyond_the_rows_scanned(self) -> None:
        buckets = [{"start": "2026-01-01", "count": 12}]

        assert self._check({"column": "at", "buckets": buckets}) == [
            _issue(
                "stats.timeline-buckets-exceed-rows-scanned",
                "timeline bucket counts sum to 12 over 10 rows scanned; the buckets and the row "
                "count were read in separate statements",
                "§2.2.16",
                "warning",
            ),
        ]

    def test_a_coverage_the_buckets_do_not_give(self) -> None:
        buckets = [{"start": "2026-01-01", "count": 4}, {"start": "2026-01-02", "count": "6"}]

        assert self._check({"column": "at", "buckets": buckets, "coverage": 1.0}) == [
            _issue(
                "stats.timeline-coverage-mismatch",
                "timeline.coverage is 1.0, but the listed bucket counts over rows_scanned computes "
                "to 0.4.",
                "§2.2.16",
            ),
        ]

    def test_a_coverage_within_the_rounding_tolerance_passes(self) -> None:
        buckets = [{"start": "2026-01-01", "count": 1}]

        assert (
            self._check({"column": "at", "buckets": buckets, "coverage": 0.3333339}, rows=3) == []
        )

    def test_a_block_that_is_not_a_mapping_is_left_to_the_schema(self) -> None:
        assert st._check_timeline({"timeline": ["at"]}, _P, self._COLUMNS, 0, True, 10) == []


class TestPopulated:
    _ANCHOR: ClassVar[dict[str, Any]] = {
        "range": {"min": "2026-01-01T00:00:00Z", "max": "2026-01-31T00:00:00Z"},
    }

    def test_populated_with_no_timeline(self) -> None:
        columns = {
            "x": "y",
            "a": {"populated": "z"},
            "b": {"populated": {"from": "2026-01-02"}},
            "c": {"populated": {}},
        }

        assert st._check_populated({}, _P, columns) == [
            _issue(
                "stats.populated-without-timeline",
                "populated is present but the file carries no timeline block to name the anchor "
                "its instants are read against.",
                "§2.2.4",
                path=f"{_P}::columns.{name}",
            )
            for name in ("b", "c")
        ]

    def test_instants_outside_the_anchors_range(self) -> None:
        columns = {
            "at": self._ANCHOR,
            "b": {"populated": {"from": "2025-12-31T00:00:00Z", "to": "2026-02-01T00:00:00Z"}},
            "c": {"populated": {"from": "2026-01-01T00:00:00Z", "to": "2026-01-31T00:00:00Z"}},
            "d": {"populated": {"from": "not an instant"}},
        }

        assert st._check_populated({"timeline": {"column": "at"}}, _P, columns) == [
            _issue(
                "stats.populated-out-of-anchor-range",
                f"populated.{key} is {value!r}, outside the anchor 'at''s own range "
                "['2026-01-01T00:00:00Z', '2026-01-31T00:00:00Z']; the two were read in separate "
                "statements.",
                "§2.2.4",
                "warning",
                f"{_P}::columns.b",
            )
            for key, value in (("from", "2025-12-31T00:00:00Z"), ("to", "2026-02-01T00:00:00Z"))
        ]

    @pytest.mark.parametrize(
        "anchor",
        [
            {"range": {"min": "2026-01-01T00:00:00Z"}},
            {"range": "x"},
            "x",
            {"range": {"min": "x", "max": "2026-01-31T00:00:00Z"}},
        ],
    )
    def test_an_anchor_without_readable_bounds_bounds_nothing(self, anchor: Any) -> None:
        columns = {"at": anchor, "b": {"populated": {"from": "1999-01-01T00:00:00Z"}}}

        assert st._check_populated({"timeline": {"column": "at"}}, _P, columns) == []

    def test_a_timeline_naming_no_column_reads_as_no_timeline(self) -> None:
        columns = {"b": {"populated": {"from": "1999-01-01T00:00:00Z"}}}

        assert [i.code for i in st._check_populated({"timeline": "x"}, _P, columns)] == [
            "stats.populated-without-timeline",
        ]
