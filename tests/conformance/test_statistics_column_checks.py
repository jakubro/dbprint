"""Per-column invariants of statistics.yaml, and the scope block they are counted over."""

from __future__ import annotations

import base64
import struct
from typing import Any, Literal

import pytest

from dbprint.conformance import statistics as st
from dbprint.conformance.issue import Issue


_P = "p.yaml"
_Severity = Literal["error", "warning"]
_C = "p.yaml::columns.c"


def _issue(
    code: str,
    detail: str,
    spec_ref: str,
    severity: _Severity = "error",
    path: str = _C,
) -> Issue:
    return Issue(path, code, severity, detail, spec_ref)


class TestCheck:
    def test_a_body_that_is_not_a_mapping(self) -> None:
        assert st.check(["columns"], _P, "s.t") == []

    def test_columns_that_are_not_a_mapping_still_get_the_table_checks(self) -> None:
        data = {
            "type": "table",
            "depends_on": [],
            "columns": ["c"],
            "null_patterns": {"patterns": []},
        }

        assert [i.code for i in st.check(data, _P, "s.t")] == ["stats.depends-on-on-table"]

    def test_counts_are_taken_over_the_scoped_rows(self) -> None:
        data = {
            "row_count": 10,
            "scope": {"rows_scanned": 4, "sample": 0.5},
            "columns": {"c": {"null_count": 5, "rows_scanned": 4}, "d": "x"},
            "null_patterns": {"patterns": [{"columns": ["c"], "count": 4}]},
        }

        assert [(i.path, i.code) for i in st.check(data, _P, "s.t")] == [
            (_C, "stats.null-count-exceeds-row-count"),
        ]

    def test_an_absent_row_count_reads_as_zero(self) -> None:
        data = {"scope": {"rows_scanned": 0}, "columns": {}}

        assert [i.code for i in st.check(data, _P, "s.t")] == ["stats.scope-asserts-nothing"]

    def test_a_scope_with_no_integer_rows_scanned_counts_over_the_table(self) -> None:
        data = {
            "row_count": 3,
            "scope": {"rows_scanned": "2", "sample": 0.5},
            "columns": {"c": {"null_count": 3}},
        }

        issues = st.check(data, _P, "s.t")

        assert "stats.null-count-exceeds-row-count" not in [i.code for i in issues]
        assert [i.code for i in issues if i.path == _C] == ["stats.population-marker-mismatch"]

    def test_the_matrix_is_read_for_a_known_classification_only(self) -> None:
        columns = {
            "c": {"classification": "boolean", "unmeasured": ["mean"]},
            "u": {"classification": "other"},
        }

        codes = {i.code for i in st.check({"row_count": 1, "columns": columns}, _P, "s.t")}

        assert codes == {
            "stats.missing-required-field-for-classification",
            "stats.unmeasured-names-unrequired-field",
        }

    def test_a_catalog_only_file_skips_the_matrix(self) -> None:
        columns = {"c": {"classification": "boolean", "unmeasured": ["mean"]}}

        assert st.check({"catalog_only": True, "columns": columns}, _P, "s.t") == [
            _issue(
                "stats.measurement-under-catalog-only",
                "catalog_only states no query was issued, but this column carries unmeasured - a "
                "measurement.",
                "§2.2.15",
            ),
        ]

    def test_every_column_check_runs(self) -> None:
        col = {
            "physical_name": "c",
            "length": {"min": 3, "avg": 1, "max": 2},
            "normalized_cardinality": 3,
            "cardinality": 2,
            "sketch": {"method": "x", "values": ""},
            "inferred": {
                "looks_like": "email",
                "looks_like_candidate": "phone",
                "sensitivity": "email",
            },
            "values": [{"count": 1}],
            "range": {"min": 5, "max": 1},
            "mean": 9,
            "percentiles": {"p10": 2, "p50": 1},
            "unrepresentable": [],
        }

        codes = {i.code for i in st.check({"row_count": 5, "columns": {"c": col}}, _P, "s.t")}

        assert codes >= {
            "stats.physical-name-matches-key",
            "stats.length-order-violated",
            "stats.normalized-cardinality-exceeds-cardinality",
            "stats.sketch-unknown-method",
            "stats.looks-like-candidate-with-verdict",
            "privacy.unredacted-sensitive",
            "stats.redacted-without-marker",
            "stats.mean-outside-range",
            "stats.percentiles-not-ordered",
            "stats.percentile-outside-range",
            "stats.unrepresentable-empty",
        }


class TestScope:
    def _check(self, scope: Any, row_count: int = 10, method: str | None = "exact") -> list[Issue]:
        return st._check_scope({"scope": scope, "row_count_method": method}, _P, row_count)

    def _scope_issue(self, code: str, detail: str, severity: _Severity = "error") -> Issue:
        return _issue(code, detail, "§2.2.8", severity, f"{_P}::scope")

    def test_no_scope(self) -> None:
        assert st._check_scope({}, _P, 10) == []

    def test_a_scope_that_is_not_a_mapping(self) -> None:
        assert self._check(["x"]) == [
            self._scope_issue("stats.scope-not-a-mapping", "scope must be a mapping, got list."),
        ]

    def test_no_rows_scanned(self) -> None:
        assert self._check({"rows_scanned": "4", "sample": 0.5}) == [
            self._scope_issue(
                "stats.scope-missing-rows-scanned",
                "scope is present but carries no integer rows_scanned.",
            ),
        ]

    def test_rows_scanned_beyond_an_exact_row_count(self) -> None:
        assert self._check({"rows_scanned": 11, "sample": 0.5}) == [
            self._scope_issue(
                "stats.scope-rows-scanned-exceeds-row-count",
                "rows_scanned=11 exceeds row_count=10 and row_count_method is 'exact'; a subset "
                "cannot be larger than the set unless the set was estimated.",
            ),
        ]

    @pytest.mark.parametrize(("rows", "method"), [(11, "approximate"), (9, None)])
    def test_rows_scanned_within_or_beyond_an_estimate(self, rows: int, method: str | None) -> None:
        assert self._check({"rows_scanned": rows, "sample": 0.5}, method=method) == []

    @pytest.mark.parametrize("sample", [0, 1.5, "0.5", -0.1])
    def test_a_sample_outside_the_unit_interval(self, sample: Any) -> None:
        assert self._check({"rows_scanned": 5, "sample": sample}) == [
            self._scope_issue(
                "stats.scope-sample-out-of-range",
                f"sample={sample!r} is outside the interval (0, 1].",
            ),
        ]

    @pytest.mark.parametrize("sample", [1, 0.01])
    def test_a_sample_inside_the_unit_interval(self, sample: Any) -> None:
        assert self._check({"rows_scanned": 5, "sample": sample}) == []

    def test_both_narrowing_keys_whatever_they_hold(self) -> None:
        assert self._check({"rows_scanned": 5, "sample": None, "filter": 3}) == [
            self._scope_issue(
                "stats.scope-sample-and-filter",
                "scope carries both sample and filter; a table is narrowed by a predicate or by "
                "a fraction, never both.",
            ),
        ]

    @pytest.mark.parametrize("scope", [{"rows_scanned": 10}, {"rows_scanned": 10, "filter": 3}])
    def test_a_scope_that_narrows_nothing(self, scope: dict[str, Any]) -> None:
        assert self._check(scope) == [
            self._scope_issue(
                "stats.scope-asserts-nothing",
                "scope covers the whole table and records neither sample nor filter; omit it.",
                "warning",
            ),
        ]

    @pytest.mark.parametrize(
        "scope",
        [{"rows_scanned": 10, "filter": "x > 1"}, {"rows_scanned": 10, "sample": 1}],
    )
    def test_a_whole_table_scope_that_records_how(self, scope: dict[str, Any]) -> None:
        assert self._check(scope) == []


class TestCatalogOnly:
    def test_each_measured_column_is_named(self) -> None:
        columns = {
            "b": "x",
            "a": {"sql_type": "int", "null_count": 1, "cardinality": 2},
            "c": {"sql_type": "int", "nullable": True},
        }

        assert st._check_catalog_only_columns(columns, _P, catalog_only=True) == [
            _issue(
                "stats.measurement-under-catalog-only",
                "catalog_only states no query was issued, but this column carries cardinality, "
                "null_count - a measurement.",
                "§2.2.15",
                path=f"{_P}::columns.a",
            ),
        ]

    def test_a_measured_file(self) -> None:
        assert (
            st._check_catalog_only_columns({"a": {"null_count": 1}}, _P, catalog_only=False) == []
        )


def _fields(issues: list[Issue], code: str) -> set[str]:
    return {i.detail.split("field ")[1].split("'")[1] for i in issues if i.code == code}


_MISSING = "stats.missing-required-field-for-classification"
_FORBIDDEN = "stats.forbidden-field-for-classification"


class TestMatrix:
    def test_a_missing_and_a_forbidden_field(self) -> None:
        col = {"classification": "boolean", "range": {}}

        issues = st._check_matrix(col, _C, "boolean", 10)

        assert (
            _issue(
                _MISSING,
                "Column with classification='boolean' is missing required field 'values'.",
                "§2.2.3",
            )
            in issues
        )
        assert (
            _issue(
                _FORBIDDEN,
                "Column with classification='boolean' MUST NOT emit field 'range'.",
                "§2.2.3",
            )
            in issues
        )
        assert _fields(issues, _FORBIDDEN) == {"range"}

    def test_a_field_named_unmeasured_is_not_missing(self) -> None:
        col = {"classification": "boolean", "unmeasured": ["values", "nullable"]}

        missing = _fields(st._check_matrix(col, _C, "boolean", 10), _MISSING)

        assert "values" not in missing
        assert "nullable" not in missing
        assert "sql_type" in missing

    def test_a_dropped_bound_is_forbidden_for_the_marker_s_reason(self) -> None:
        col = {"classification": "numeric", "redacted": "drop", "range": {}}

        issues = st._check_matrix(col, _C, "numeric", 10)

        assert [i for i in issues if i.code == _FORBIDDEN] == [
            _issue(
                _FORBIDDEN,
                "Column with classification='numeric' MUST NOT emit field 'range', since the "
                "column declares redacted: drop, which emits no literal.",
                "§2.2.9",
            ),
        ]
        assert (
            _fields(issues, _MISSING) & {"range", "percentiles", "mean", "sum", "zero_count"}
            == set()
        )

    def test_prose_publishes_no_value_list(self) -> None:
        col = {"classification": "text", "inferred": {"looks_like": "prose"}, "values": []}

        issues = st._check_matrix(col, _C, "text", 10)

        assert [i.spec_ref for i in issues if i.code == _FORBIDDEN] == ["§2.2.3"]
        assert _fields(issues, _FORBIDDEN) == {"values"}
        assert _fields(issues, _MISSING) & {"values_coverage", "distribution"} == set()

    @pytest.mark.parametrize(
        ("sql_type", "null_count", "length_forbidden"),
        [
            ("integer", 0, True),
            ("varchar(10)", 10, True),
            ("varchar(10)", 9, False),
            ("varchar(10)", "10", False),
            (None, 0, True),
        ],
    )
    def test_length_follows_the_value_type(
        self,
        sql_type: Any,
        null_count: Any,
        length_forbidden: bool,
    ) -> None:
        col = {"classification": "categorical", "sql_type": sql_type, "null_count": null_count}

        issues = st._check_matrix({**col, "length": {}}, _C, "categorical", 10)
        absent = st._check_matrix(col, _C, "categorical", 10)

        assert ("length" in _fields(issues, _FORBIDDEN)) is length_forbidden
        assert ("length" in _fields(absent, _MISSING)) is not length_forbidden

    @pytest.mark.parametrize(
        ("sql_type", "forbidden"),
        [("date", True), ("timestamp", False), (5, True)],
    )
    def test_quantized_count_follows_day_resolution(self, sql_type: Any, forbidden: bool) -> None:
        col = {"classification": "temporal", "sql_type": sql_type, "quantized_count": 0}

        assert (
            "quantized_count" in _fields(st._check_matrix(col, _C, "temporal", 10), _FORBIDDEN)
        ) is forbidden

    def test_the_inferred_rows(self) -> None:
        col = {
            "classification": "boolean",
            "inferred": {"looks_like": "x", "epoch_unit": "s", "candidate_key": True},
        }

        assert st._check_inferred_matrix(col, _C, "boolean") == [
            _issue(
                _FORBIDDEN,
                f"Column with classification='boolean' MUST NOT emit field 'inferred.{name}'.",
                "§2.2.3",
            )
            for name in ("epoch_unit", "looks_like")
        ]

    @pytest.mark.parametrize(
        ("inferred", "classification"),
        [("x", "boolean"), ({"looks_like": "x"}, "unsupported"), ({"looks_like": "x"}, "text")],
    )
    def test_inferred_rows_that_do_not_apply(self, inferred: Any, classification: str) -> None:
        assert st._check_inferred_matrix({"inferred": inferred}, _C, classification) == []


class TestUnmeasured:
    def test_an_emitted_and_an_unrequired_name(self) -> None:
        col = {"classification": "boolean", "unmeasured": ["values", "mean", 5], "values": []}

        assert st._check_unmeasured(col, _C, "boolean", 10) == [
            _issue(
                "stats.unmeasured-names-unrequired-field",
                "unmeasured names 'mean', which classification='boolean' does not require; its "
                "absence needs no marker.",
                "§2.2.4",
            ),
            _issue(
                "stats.unmeasured-names-emitted-field",
                "unmeasured names 'values', which this column also emits.",
                "§2.2.4",
            ),
        ]

    def test_a_field_a_conditional_cell_forbids_is_not_required(self) -> None:
        col = {"classification": "numeric", "redacted": "drop", "unmeasured": ["range"]}

        assert [i.code for i in st._check_unmeasured(col, _C, "numeric", 10)] == [
            "stats.unmeasured-names-unrequired-field",
        ]

    def test_a_required_field(self) -> None:
        assert st._check_unmeasured({"unmeasured": ["range"]}, _C, "numeric", 10) == []

    @pytest.mark.parametrize(("classification", "n"), [("text", 0), ("boolean", 1)])
    def test_a_sample_verdict_is_required_where_sampled(self, classification: str, n: int) -> None:
        col = {"unmeasured": ["inferred.looks_like"]}

        assert len(st._check_unmeasured(col, _C, classification, 10)) == n

    @pytest.mark.parametrize("unmeasured", [None, "values", []])
    def test_no_names(self, unmeasured: Any) -> None:
        assert (
            st._check_unmeasured({"unmeasured": unmeasured, "values": []}, _C, "boolean", 10) == []
        )


class TestTableUnmeasured:
    def test_emitted_blocks_are_named_in_order(self) -> None:
        data = {
            "unmeasured": ["timeline", "grain", "dependencies", 3],
            "timeline": {},
            "grain": {},
            "dependencies": None,
        }

        assert st._check_table_unmeasured(data, _P) == [
            _issue(
                "stats.unmeasured-names-emitted-block",
                f"unmeasured names {name!r}, which this file also emits.",
                "§2.2.1",
                path=_P,
            )
            for name in ("grain", "timeline")
        ]

    def test_a_marker_that_is_not_a_list(self) -> None:
        assert st._check_table_unmeasured({"unmeasured": "grain", "grain": {}}, _P) == []


_KEY = {"cardinality": 10, "cardinality_method": "exact", "null_count": 0}


class TestCandidateKey:
    def test_a_key_left_unmarked(self) -> None:
        assert st._check_candidate_key({**_KEY, "cardinality_ratio": 1}, _C, 10) == [
            _issue(
                "stats.candidate-key-mismatch",
                "inferred.candidate_key=None disagrees with the recomputed value True for "
                "cardinality_ratio=1.",
                "§4.2",
            ),
        ]

    def test_a_non_key_marked_false(self) -> None:
        col = {**_KEY, "cardinality_ratio": 0.5, "inferred": {"candidate_key": False}}

        assert st._check_candidate_key(col, _C, 10) == [
            _issue(
                "stats.candidate-key-mismatch",
                "inferred.candidate_key=False disagrees with the recomputed value None for "
                "cardinality_ratio=0.5.",
                "§4.2",
            ),
        ]

    def test_a_missing_exception(self) -> None:
        col = {
            **_KEY,
            "cardinality": 9,
            "cardinality_ratio": 0.99995,
            "inferred": {"candidate_key": True},
        }

        assert st._check_candidate_key(col, _C, 10) == [
            _issue(
                "stats.candidate-key-exception-mismatch",
                "inferred.candidate_key_exception=None disagrees with the recomputed value "
                "'measured_duplicates'.",
                "§4.2",
            ),
        ]

    @pytest.mark.parametrize(
        ("col", "exception"),
        [
            ({**_KEY, "cardinality": 9, "cardinality_ratio": 0.99995, "null_count": 1}, None),
            (
                {**_KEY, "cardinality_method": "approximate", "cardinality_ratio": 0.99995},
                "estimated",
            ),
        ],
    )
    def test_an_agreeing_marker(self, col: dict[str, Any], exception: str | None) -> None:
        inferred = {"candidate_key": True, "candidate_key_exception": exception}

        assert st._check_candidate_key({**col, "inferred": inferred}, _C, 10) == []

    @pytest.mark.parametrize(
        "change",
        [
            {"cardinality": "10"},
            {"cardinality_ratio": True},
            {"cardinality_ratio": "1"},
            {"cardinality_method": 1},
            {"null_count": None},
        ],
    )
    def test_an_unreadable_input_decides_nothing(self, change: dict[str, Any]) -> None:
        assert st._check_candidate_key({**_KEY, "cardinality_ratio": 1.0, **change}, _C, 10) == []


class TestPopulationMarker:
    @pytest.mark.parametrize(
        ("marker", "scoped", "expected"),
        [(4, True, 5), (5, False, None), (None, True, 5)],
    )
    def test_a_marker_that_disagrees_with_scope(
        self,
        marker: Any,
        scoped: bool,
        expected: Any,
    ) -> None:
        assert st._check_population_marker(
            {"rows_scanned": marker},
            _C,
            scoped=scoped,
            rows_scanned=5,
        ) == [
            _issue(
                "stats.population-marker-mismatch",
                f"rows_scanned={marker!r} but the file's scope requires {expected!r}.",
                "§2.2.8",
            ),
        ]

    @pytest.mark.parametrize(("col", "scoped"), [({"rows_scanned": 5}, True), ({}, False)])
    def test_an_agreeing_marker(self, col: dict[str, Any], scoped: bool) -> None:
        assert st._check_population_marker(col, _C, scoped=scoped, rows_scanned=5) == []


def _counts(col: dict[str, Any], rows: int = 10) -> list[Issue]:
    return st._check_count_invariants(col, _C, rows)


class TestCountInvariants:
    def test_more_nulls_than_rows(self) -> None:
        assert _counts({"null_count": 11}) == [
            _issue(
                "stats.null-count-exceeds-row-count",
                "null_count=11 exceeds the scanned row count 10.",
                "§2.2.7",
            ),
        ]

    def test_a_null_in_a_non_nullable_column(self) -> None:
        assert _counts({"null_count": 1, "nullable": False}) == [
            _issue(
                "stats.nullable-contradicts-null-count",
                "nullable=False but null_count=1; a column declared non-nullable cannot have "
                "scanned a NULL.",
                "§2.2.2",
            ),
        ]

    @pytest.mark.parametrize(
        "col",
        [
            {"null_count": 10},
            {"null_count": 0, "nullable": False},
            {"null_count": 1, "nullable": None},
            {"null_count": "1", "nullable": False},
        ],
    )
    def test_consistent_null_counts(self, col: dict[str, Any]) -> None:
        assert _counts(col) == []

    @pytest.mark.parametrize(("cardinality", "null_count", "shown"), [(9, 2, 8), (1, 12, -2)])
    def test_more_distinct_values_than_non_null_rows(
        self,
        cardinality: int,
        null_count: int,
        shown: int,
    ) -> None:
        issues = _counts({"cardinality": cardinality, "null_count": null_count})

        assert (
            _issue(
                "stats.cardinality-exceeds-row-count",
                f"cardinality={cardinality} exceeds the non-null scanned count {shown}.",
                "§2.2.7",
            )
            in issues
        )

    @pytest.mark.parametrize(("cardinality", "null_count"), [(8, 2), (0, 12), ("9", 2)])
    def test_a_cardinality_within_the_non_null_rows(
        self,
        cardinality: Any,
        null_count: int,
    ) -> None:
        assert "stats.cardinality-exceeds-row-count" not in [
            i.code for i in _counts({"cardinality": cardinality, "null_count": null_count})
        ]

    @pytest.mark.parametrize(
        "field",
        ["zero_count", "negative_count", "empty_count", "quantized_count"],
    )
    def test_a_degenerate_count_beyond_the_non_null_rows(self, field: str) -> None:
        assert _counts({"null_count": 2, field: 9}) == [
            _issue(
                "stats.degenerate-count-exceeds-row-count",
                f"{field}={9} exceeds the non-null scanned count 8.",
                "§2.2.7",
            ),
        ]

    @pytest.mark.parametrize(
        "col",
        [
            {"null_count": 2, "zero_count": 8},
            {"null_count": 2, "zero_count": True},
            {"null_count": "2", "zero_count": 9},
        ],
    )
    def test_a_degenerate_count_within_bounds(self, col: dict[str, Any]) -> None:
        assert _counts(col) == []

    def test_no_non_null_rows_bound_a_degenerate_count_at_zero(self) -> None:
        assert [i.detail for i in _counts({"null_count": 12, "zero_count": 1}, rows=12)] == [
            "zero_count=1 exceeds the non-null scanned count 0.",
        ]

    def test_a_null_rate_that_disagrees(self) -> None:
        assert _counts({"null_count": 2, "null_rate": 0.200002}) == [
            _issue(
                "stats.null-rate-mismatch",
                "null_rate=0.200002 disagrees with null_count=2 of rows_scanned=10 (0.2).",
                "§2.2.6",
            ),
        ]

    @pytest.mark.parametrize(
        "col",
        [
            {"null_count": 2, "null_rate": 0.2000009},
            {"null_count": 2, "null_rate": True},
            {"null_count": None, "null_rate": 0.9},
        ],
    )
    def test_a_null_rate_that_agrees_or_is_unreadable(self, col: dict[str, Any]) -> None:
        assert _counts(col) == []

    def test_a_cardinality_ratio_that_disagrees(self) -> None:
        assert _counts({"cardinality": 5, "cardinality_ratio": 0.499998}) == [
            _issue(
                "stats.cardinality-ratio-mismatch",
                "cardinality_ratio=0.499998 disagrees with cardinality=5 of rows_scanned=10 (0.5).",
                "§2.2.6",
            ),
        ]

    @pytest.mark.parametrize(
        "col",
        [
            {"cardinality": 5, "cardinality_ratio": 0.4999991},
            {"cardinality": 5, "cardinality_ratio": True},
            {"cardinality": "5", "cardinality_ratio": 0.1},
        ],
    )
    def test_a_cardinality_ratio_that_agrees_or_is_unreadable(self, col: dict[str, Any]) -> None:
        assert _counts(col) == []


def _values(*counts: Any) -> list[dict[str, Any]]:
    return [{"value": f"v{i}", "count": c} for i, c in enumerate(counts)]


class TestValueCounts:
    def test_an_exhaustive_list_that_does_not_add_up(self) -> None:
        assert _counts({"null_count": 2, "values": _values(4, 3, "x"), "values_coverage": 1.0}) == [
            _issue(
                "stats.values-sum-mismatch",
                "values_coverage is 1.0 but the listed counts (7) do not equal the non-null "
                "scanned count (8).",
                "§2.2.4",
                "warning",
            ),
        ]

    def test_a_truncated_list_beyond_the_non_null_rows(self) -> None:
        assert _counts({"null_count": 2, "values": _values(5, 4)}) == [
            _issue(
                "stats.values-sum-mismatch",
                "listed value counts (9) exceed the non-null scanned count (8); values and "
                "null_count were read in separate statements.",
                "§2.2.4",
                "warning",
            ),
        ]

    def test_a_coverage_the_counts_do_not_give(self) -> None:
        assert _counts({"null_count": 2, "values": _values(4), "values_coverage": 0.500002}) == [
            _issue(
                "stats.values-coverage-mismatch",
                "values_coverage=0.500002 disagrees with the listed counts (4 of 8 = 0.5).",
                "§2.2.4",
                "warning",
            ),
        ]

    @pytest.mark.parametrize(
        "col",
        [
            {"null_count": 2, "values": _values(4), "values_coverage": 0.5000009},
            {"null_count": 2, "values": _values(4), "values_coverage": True},
            {"null_count": 10, "values": _values(), "values_coverage": 0.3},
            {"null_count": 2, "values": _values(8), "values_coverage": 1.0},
            {"null_count": "2", "values": _values(20), "values_coverage": 0.3},
            {"null_count": 2, "values": "x", "values_coverage": 0.3},
        ],
    )
    def test_consistent_or_unreadable_value_counts(self, col: dict[str, Any]) -> None:
        assert _counts(col) == []

    def _exhaustive(self, cardinality: int, **extra: Any) -> list[Issue]:
        col = {
            "null_count": 0,
            "values": _values(4, 6),
            "values_coverage": 1.0,
            "cardinality": cardinality,
            "cardinality_method": "exact",
            **extra,
        }

        return _counts(col)

    @pytest.mark.parametrize(
        ("cardinality", "extra", "code", "severity"),
        [
            (3, {}, "stats.values-list-short-of-cardinality", "error"),
            (
                3,
                {"values_coverage_method": "bounded"},
                "stats.values-list-short-of-cardinality-bounded",
                "warning",
            ),
            (1, {}, "stats.values-list-exceeds-cardinality", "warning"),
        ],
    )
    def test_an_exhaustive_list_of_the_wrong_length(
        self,
        cardinality: int,
        extra: dict[str, Any],
        code: str,
        severity: _Severity,
    ) -> None:
        assert self._exhaustive(cardinality, **extra) == [
            _issue(
                code,
                f"the values list carries 2 entries but cardinality is {cardinality}; an "
                "exhaustive list must carry exactly cardinality entries.",
                "§2.2.4",
                severity,
            ),
        ]

    @pytest.mark.parametrize(
        ("cardinality", "extra"),
        [
            (2, {}),
            (3, {"cardinality_method": "approximate"}),
            (3, {"values_coverage": 0.999999, "values": _values(4, 5)}),
        ],
    )
    def test_a_list_whose_length_is_not_checked(
        self,
        cardinality: int,
        extra: dict[str, Any],
    ) -> None:
        issues = self._exhaustive(cardinality, **extra)

        assert [i.code for i in issues if "cardinality" in i.code] == []


class _Number:
    def __init__(self, source: str) -> None:
        self.source = source


_UNORDERED = [
    _issue(
        "stats.values-not-ordered",
        "values is not ordered by count descending with ties by the published text of value.",
        "§2.2.4",
    ),
]


class TestValueOrder:
    @pytest.mark.parametrize(
        ("values", "redacted"),
        [
            ([{"value": "b", "count": 1}, {"value": "a", "count": 2}], None),
            ([{"value": "b", "count": 1}, {"value": "a", "count": 1}], None),
            ([{"value": "b", "count": 1}, {"value": "a", "count": 1}], "hash"),
            ([{"value": _Number("9"), "count": 1}, {"value": _Number("10"), "count": 1}], None),
            ([{"count": 1}, {"count": 2}], "mask"),
        ],
    )
    def test_an_unordered_list(self, values: list[Any], redacted: str | None) -> None:
        assert st._check_value_order({"values": values, "redacted": redacted}, _C) == _UNORDERED

    @pytest.mark.parametrize(
        ("values", "redacted"),
        [
            ([{"value": "a", "count": 2}, {"value": "b", "count": 1}], None),
            ([{"value": "b", "count": 1}, {"value": "a", "count": 1}], "mask"),
            ([{"value": "b", "count": 1}, {"value": "a", "count": "2"}], None),
            ([{"value": "b", "count": 1}, "a"], None),
            ([{"value": "b", "count": 1}], None),
            ("ab", None),
        ],
    )
    def test_an_ordered_or_unreadable_list(self, values: Any, redacted: str | None) -> None:
        assert st._check_value_order({"values": values, "redacted": redacted}, _C) == []

    def test_a_value_with_no_text_form_orders_by_its_string(self) -> None:
        values = [{"value": {"b": 1}, "count": 1}, {"value": {"a": 1}, "count": 1}]

        assert st._check_value_order({"values": values}, _C) == _UNORDERED


class TestSpellingGroups:
    def test_consistent_spellings(self) -> None:
        values = [
            "x",
            {"value": "Foo", "count": 2},
            {"value": " foo", "spelling_of": "Foo", "count": 1},
        ]

        assert st._check_spelling_groups({"values": values}, _C) == []

    @pytest.mark.parametrize("target", ["Bar", "foo "])
    def test_a_target_that_is_not_a_canonical_entry(self, target: str) -> None:
        values = [
            {"value": "Foo"},
            {"value": "foo ", "spelling_of": "Foo"},
            {"value": "FOO", "spelling_of": target},
        ]

        assert st._check_spelling_groups({"values": values}, _C) == [
            _issue(
                "stats.spelling-of-target-unlisted",
                f"values entry 'FOO' carries spelling_of={target!r}, which is not a listed value "
                "without a spelling_of of its own.",
                "§2.2.4",
            ),
        ]

    @pytest.mark.parametrize(("value", "target"), [("xyz", "Foo"), (1, "1"), ("1", 1)])
    def test_a_spelling_that_does_not_fold_to_its_target(self, value: Any, target: Any) -> None:
        values = [
            {"value": "Foo"},
            {"value": 1},
            {"value": "1"},
            {"value": value, "spelling_of": target},
        ]

        assert st._check_spelling_groups({"values": values}, _C) == [
            _issue(
                "stats.spelling-of-key-mismatch",
                f"values entry {value!r} carries spelling_of={target!r} but the two do not fold "
                "to one key (trimmed, lower-cased).",
                "§2.2.4",
            ),
        ]

    def test_every_entry_is_read(self) -> None:
        values = [{"value": "a", "spelling_of": "x"}, {"value": "b", "spelling_of": "y"}]

        assert len(st._check_spelling_groups({"values": values}, _C)) == 2

    def test_no_list(self) -> None:
        assert st._check_spelling_groups({"values": "a"}, _C) == []


def _distribution(distribution: Any, *counts: Any, coverage: Any = 1.0) -> list[Issue]:
    col = {
        "distribution": distribution,
        "values": [{"count": c} for c in counts],
        "values_coverage": coverage,
    }

    return st._check_distribution(col, _C)


class TestDistribution:
    @pytest.mark.parametrize(
        ("counts", "expected"),
        [
            ((95, 5), "dominant_value"),
            ((3, 0, 3), "imbalanced"),
            ((0, 3, 3), "imbalanced"),
            ((-50, 60, 30), "uniform"),
            ((5, 4, 2), "imbalanced"),
            ((4, 2, 2), "uniform"),
            ((1, 0, 1), "uniform"),
        ],
    )
    def test_the_verdict_recomputed_from_the_list(
        self,
        counts: tuple[int, ...],
        expected: str,
    ) -> None:
        wrong = "long_tail"

        assert _distribution(wrong, *counts) == [
            _issue(
                "stats.distribution-mismatch",
                f"distribution={wrong!r} disagrees with the value list; expected {expected!r}.",
                "§2.2.5",
                "warning",
            ),
        ]
        assert _distribution(expected, *counts) == []

    def test_a_share_just_below_dominance(self) -> None:
        assert [i.detail for i in _distribution("dominant_value", 189, 11)] == [
            "distribution='dominant_value' disagrees with the value list; expected 'imbalanced'.",
        ]

    @pytest.mark.parametrize(
        ("distribution", "counts", "coverage"),
        [
            ("x", (1, 2), 0.5),
            (5, (1, 2), 1.0),
            ("x", ("1", None), 1.0),
            ("x", (0, 0), 1.0),
            ("x", (), 1.0),
        ],
    )
    def test_an_unverifiable_list(
        self,
        distribution: Any,
        counts: tuple[Any, ...],
        coverage: Any,
    ) -> None:
        assert _distribution(distribution, *counts, coverage=coverage) == []


def _frequencies(
    distribution: Any,
    top: Any,
    bottom: Any,
    listed: Any,
    total: Any,
    **col: Any,
) -> list[Issue]:
    base = {
        "distribution": distribution,
        "frequencies": {"top": top, "bottom": bottom, "listed": listed, "total": total},
        "cardinality_method": "exact",
        "null_count": 0,
        "cardinality": 50,
        **col,
    }

    return st._check_frequencies_distribution(base, _C, 100)


class TestFrequenciesDistribution:
    @pytest.mark.parametrize(
        ("counts", "col", "expected"),
        [
            ((95, 1, 3, 97), {}, "dominant_value"),
            ((10, 1, 3, 20), {}, "long_tail"),
            ((10, 1, 3, 30), {}, "imbalanced"),
            ((10, 5, 3, 30), {}, "uniform"),
            ((11, 5, 3, 30), {}, "imbalanced"),
            ((10, 0, 3, 30), {}, "uniform"),
            ((50, 50, 1, 50), {"cardinality": 1}, "dominant_value"),
            ((10, 1, 3, 20), {"cardinality": 3}, "imbalanced"),
            ((96, 48, 3, 101), {}, "uniform"),
            ((96, 1, 3, 101), {}, "imbalanced"),
            ((5, 1, 0, 5), {}, "uniform"),
            ((5, 1, 3, 5), {"null_count": 100}, "uniform"),
            ((5, 1, 3, 5), {"null_count": 120}, "uniform"),
        ],
    )
    def test_the_verdict_recomputed_from_the_summary(
        self,
        counts: tuple[int, ...],
        col: dict[str, Any],
        expected: str,
    ) -> None:
        wrong = "wrong"

        assert _frequencies(wrong, *counts, **col) == [
            _issue(
                "stats.distribution-contradicts-frequencies",
                f"distribution={wrong!r} disagrees with frequencies (top={counts[0]}, "
                f"bottom={counts[1]}, listed={counts[2]}, total={counts[3]}); expected {expected!r}.",
                "§2.2.5",
            ),
        ]
        assert _frequencies(expected, *counts, **col) == []

    @pytest.mark.parametrize(
        ("counts", "col"),
        [
            ((10, 1, 3, 20), {"cardinality_method": "approximate"}),
            ((10, 1, 3, 20), {"null_count": True}),
            ((10, 1, 3, 20), {"null_count": "0"}),
            ((10, 1, 3, 20), {"cardinality": True}),
            ((10, 1, 3, 20), {"cardinality": "50"}),
            ((10, True, 3, 20), {}),
            ((10, 1, "3", 20), {}),
            ((10, 1, 3, 20), {"distribution": 5}),
            ((10, 1, 3, 20), {"frequencies": "x"}),
        ],
    )
    def test_an_unverifiable_summary(self, counts: tuple[Any, ...], col: dict[str, Any]) -> None:
        rest = {k: v for k, v in col.items() if k != "distribution"}

        assert _frequencies(col.get("distribution", "wrong"), *counts, **rest) == []


class TestRedactionMarkers:
    def test_an_entry_with_no_value_and_no_marker(self) -> None:
        assert st._check_redaction_marker({"values": ["x", {"value": "a"}, {"count": 1}]}, _C) == [
            _issue(
                "stats.redacted-without-marker",
                "a values entry carries no `value` but the column declares no `redacted` primitive.",
                "§2.2.9",
            ),
        ]

    @pytest.mark.parametrize(
        "col",
        [
            {"values": [{"count": 1}], "redacted": "mask"},
            {"values": [{"value": "a"}, "x"]},
            {"values": "x"},
        ],
    )
    def test_a_marked_or_literal_list(self, col: dict[str, Any]) -> None:
        assert st._check_redaction_marker(col, _C) == []

    def test_a_sensitive_column_publishing_values(self) -> None:
        col = {"inferred": {"sensitivity": "email"}, "values": [], "percentiles": {}, "mean": 1}

        assert st._check_unredacted_sensitive(col, _C) == [
            _issue(
                "privacy.unredacted-sensitive",
                "column declares inferred.sensitivity='email' and publishes values, percentiles "
                "with no redacted primitive covering it.",
                "§4.4",
                "warning",
            ),
        ]

    def test_the_range_is_publication(self) -> None:
        assert (
            len(st._check_unredacted_sensitive({"inferred": {"sensitivity": "x"}, "range": {}}, _C))
            == 1
        )

    @pytest.mark.parametrize(
        "col",
        [
            {"inferred": {"sensitivity": "email"}, "values": [], "redacted": "mask"},
            {"inferred": {"sensitivity": "email"}, "mean": 1},
            {"inferred": {"sensitivity": 5}, "values": []},
            {"inferred": "email", "values": []},
        ],
    )
    def test_a_covered_or_silent_column(self, col: dict[str, Any]) -> None:
        assert st._check_unredacted_sensitive(col, _C) == []

    def test_uncoarsened_day_counts(self) -> None:
        col = {"redacted": "mask", "freshness": {"max_age_days": 91}, "range": {"span_days": 181}}

        assert st._check_redacted_day_counts(col, _C) == [
            _issue(
                "stats.uncoarsened-redacted-day-count",
                f"{field}={value} is not a multiple of 90 on a column declaring redacted.",
                "§2.2.9",
            )
            for field, value in (("freshness.max_age_days", 91), ("range.span_days", 181))
        ]

    @pytest.mark.parametrize(
        "col",
        [
            {"freshness": {"max_age_days": 91}},
            {"redacted": "drop", "freshness": {"max_age_days": 180}, "range": {"span_days": 0}},
            {"redacted": "drop", "freshness": {"max_age_days": "91"}, "range": "x"},
            {"redacted": "drop", "freshness": "x", "range": {"span_days": 90}},
        ],
    )
    def test_coarsened_or_unredacted_day_counts(self, col: dict[str, Any]) -> None:
        assert st._check_redacted_day_counts(col, _C) == []


def _precision(field: str, value: Any, detail: str) -> Issue:
    return _issue(
        "stats.excess-precision",
        f"{field}={value!r} is not what SPEC 2.2.6 emits for it: {detail}.",
        "§2.2.6",
    )


class TestPrecision:
    def test_every_field_is_read(self) -> None:
        col = {
            "null_rate": 0.1234567,
            "cardinality_ratio": 1.23457e-07,
            "values_coverage": 0.25,
            "mean": 1.23456789,
            "sum": 2.0000001,
            "range": {"min": 0.1234567, "max": 3.0000001},
            "percentiles": {"p50": 0.5000001, "p90": 1.23457e-07},
        }

        assert st._check_precision(col, _C) == [
            _precision("null_rate", 0.1234567, "0.123457"),
            _precision("cardinality_ratio", 1.23457e-07, "0.0"),
            _precision("mean", 1.23456789, "1.234568"),
            _precision("sum", 2.0000001, "2.0"),
            _precision("range.min", 0.1234567, "0.123457"),
            _precision("range.max", 3.0000001, "3.0"),
            _precision("percentiles.p50", 0.5000001, "0.5"),
        ]

    @pytest.mark.parametrize("value", [float("inf"), float("nan"), 3, "0.1234567"])
    def test_values_with_no_decimals_to_count(self, value: Any) -> None:
        assert st._check_precision({"mean": value, "null_rate": value}, _C) == []

    def test_blocks_that_are_not_mappings(self) -> None:
        assert st._check_precision({"range": [0.1234567], "percentiles": [0.1234567]}, _C) == []


class TestUnrepresentable:
    def test_an_empty_list(self) -> None:
        assert st._check_unrepresentable({"unrepresentable": []}, _C) == [
            _issue(
                "stats.unrepresentable-empty",
                "unrepresentable is an empty list; omit the key instead.",
                "§2.2.4",
            ),
        ]

    def test_names_the_column_did_not_emit(self) -> None:
        col = {
            "unrepresentable": ["min", "p50", "max", "p90", 3],
            "range": {"min": "x"},
            "percentiles": {"p50": "y"},
        }

        assert st._check_unrepresentable(col, _C) == [
            _issue(
                "stats.unrepresentable-names-unemitted-field",
                f"unrepresentable names {name!r}, which the column did not emit.",
                "§2.2.4",
            )
            for name in ("max", "p90")
        ]

    def test_blocks_that_are_not_mappings_emit_nothing(self) -> None:
        col = {"unrepresentable": ["min"], "range": ["min"], "percentiles": ["min"]}

        assert len(st._check_unrepresentable(col, _C)) == 1

    def test_no_list(self) -> None:
        assert st._check_unrepresentable({"unrepresentable": "min"}, _C) == []


_JAN_1 = "2026-01-01T00:00:00Z"
_JAN_11 = "2026-01-11T00:00:00Z"


class TestSpanDays:
    @pytest.mark.parametrize("col", [{}, {"redacted": "drop"}, {"unrepresentable": ["p50"]}])
    def test_a_span_that_disagrees_with_the_bounds(self, col: dict[str, Any]) -> None:
        full = {**col, "range": {"min": _JAN_1, "max": _JAN_11, "span_days": 9}}

        assert st._check_span_days(full, _C) == [
            _issue(
                "stats.span-days-mismatch",
                "span_days=9 but day_count(range.min, range.max)=10.",
                "§2.2.4",
            ),
        ]

    @pytest.mark.parametrize(
        ("col", "rng"),
        [
            ({}, {"min": _JAN_1, "max": _JAN_11, "span_days": 10}),
            ({"redacted": "mask"}, {"min": _JAN_1, "max": _JAN_11, "span_days": 9}),
            ({"redacted": "hash"}, {"min": _JAN_1, "max": _JAN_11, "span_days": 9}),
            ({"unrepresentable": ["min"]}, {"min": _JAN_1, "max": _JAN_11, "span_days": 9}),
            ({"unrepresentable": ["max"]}, {"min": _JAN_1, "max": _JAN_11, "span_days": 9}),
            ({}, {"min": "x", "max": _JAN_11, "span_days": 9}),
            ({}, {"min": _JAN_1, "max": "x", "span_days": 9}),
            ({}, {"min": _JAN_1, "max": _JAN_11, "span_days": "9"}),
            ({}, "x"),
        ],
    )
    def test_a_span_that_agrees_or_cannot_be_read_back(self, col: dict[str, Any], rng: Any) -> None:
        assert st._check_span_days({**col, "range": rng}, _C) == []


class TestMaxAgeDays:
    def _check(
        self,
        observed: Any,
        max_: str = _JAN_1,
        profiled_at: Any = _JAN_11,
        **col: Any,
    ) -> list[Issue]:
        full = {"freshness": {"max_age_days": observed}, "range": {"max": max_}, **col}

        return st._check_max_age_days_mismatch(full, _C, profiled_at)

    @pytest.mark.parametrize(("observed", "max_", "expected"), [(9, _JAN_1, 10), (1, _JAN_11, 0)])
    def test_an_age_that_disagrees(self, observed: int, max_: str, expected: int) -> None:
        assert self._check(observed, max_, profiled_at=_JAN_1 if max_ == _JAN_11 else _JAN_11) == [
            _issue(
                "stats.max-age-days-mismatch",
                f"freshness.max_age_days={observed} but max(0, day_count(range.max, profiled_at))={expected}.",
                "§2.2.4",
            ),
        ]

    def test_an_age_that_agrees(self) -> None:
        assert self._check(10) == []
        assert self._check(9, unrepresentable=["min"]) != []

    @pytest.mark.parametrize(
        ("observed", "col", "profiled_at"),
        [
            (9, {"redacted": "mask"}, _JAN_11),
            (9, {"unrepresentable": ["max"]}, _JAN_11),
            ("9", {}, _JAN_11),
            (9, {}, "x"),
            (9, {"range": "x"}, _JAN_11),
            (9, {"freshness": "x"}, _JAN_11),
        ],
    )
    def test_an_age_that_cannot_be_read_back(
        self,
        observed: Any,
        col: dict[str, Any],
        profiled_at: Any,
    ) -> None:
        assert self._check(observed, profiled_at=profiled_at, **col) == []

    def test_a_bound_that_cannot_be_read_back(self) -> None:
        assert self._check(9, max_="x") == []


_BIG = 2.0**60


class TestPercentiles:
    def _order(self, percentiles: Any, classification: str = "numeric") -> list[Issue]:
        return st._check_percentiles_order(
            {"percentiles": percentiles, "classification": classification},
            _C,
        )

    def test_a_descending_pair_is_named(self) -> None:
        assert self._order({"p90": 2, "p5": 3, "x": 0, "pz": 0, 7: 0, "p10": 4}) == [
            _issue(
                "stats.percentiles-not-ordered",
                "percentiles.p10=4 exceeds percentiles.p90=2; percentiles must ascend with their keys.",
                "§2.2.4",
            ),
        ]

    @pytest.mark.parametrize(
        ("percentiles", "classification", "n"),
        [
            ({"p10": 1.0000009, "p50": 1.0}, "numeric", 0),
            ({"p10": 1.000002, "p50": 1.0}, "numeric", 1),
            ({"p10": _BIG + 1024, "p50": _BIG}, "numeric", 0),
            ({"p10": _BIG + 2048, "p50": _BIG}, "numeric", 1),
            ({"p10": 2.0**52 + 2, "p50": 2.0**52}, "numeric", 1),
            ({"p10": 2.0**53 + 4, "p50": 2.0**53}, "numeric", 1),
            ({"p10": "12000-06-01", "p50": "12000-01-01"}, "temporal", 0),
            ({"p10": _JAN_11, "p50": _JAN_1}, "temporal", 1),
            ({"p10": _JAN_1, "p50": _JAN_1}, "temporal", 0),
            ({"p10": "12000-01-01", "p50": "11000-01-01"}, "temporal", 1),
            ({"p10": "11000-01-01", "p50": "12000-01-01"}, "temporal", 0),
            ({"p10": _JAN_11, "p50": "11000-01-01"}, "temporal", 0),
            ({"p10": 3, "p50": "2"}, "numeric", 0),
            ({}, "numeric", 0),
            ("p10", "numeric", 0),
        ],
    )
    def test_ascending_percentiles(self, percentiles: Any, classification: str, n: int) -> None:
        assert len(self._order(percentiles, classification)) == n

    def _contain(self, percentiles: dict[str, Any], lo: Any, hi: Any, **col: Any) -> list[Issue]:
        full = {
            "classification": "numeric",
            "percentiles": percentiles,
            "range": {"min": lo, "max": hi},
            **col,
        }

        return st._check_percentiles_containment(full, _C)

    def test_percentiles_outside_the_range(self) -> None:
        assert self._contain({"p5": -1, "p50": 5, "p90": 11}, 0, 10) == [
            _issue(
                "stats.percentile-outside-range",
                f"percentiles.{key}={value!r} lies outside range [0, 10].",
                "§2.2.4",
            )
            for key, value in (("p05", -1), ("p90", 11))
        ]

    @pytest.mark.parametrize(
        ("percentiles", "lo", "hi", "n"),
        [
            ({"p50": -0.0000009, "p90": 10.0000009}, 0, 10, 0),
            ({"p50": -0.000002}, 0, 10, 1),
            ({"p50": 10.000002}, 0, 10, 1),
            ({"p50": _BIG - 1024}, _BIG, _BIG, 0),
            ({"p50": _BIG + 1024}, _BIG, _BIG, 0),
            ({"p50": _BIG + 2048}, _BIG, _BIG, 1),
            ({"p50": _BIG - 2048}, _BIG, _BIG, 1),
            ({"p50": 2.0**52 + 2}, 2.0**52, 2.0**52, 1),
            ({"p50": 2.0**52 - 2}, 2.0**52, 2.0**52, 1),
        ],
    )
    def test_numeric_tolerance(self, percentiles: dict[str, Any], lo: Any, hi: Any, n: int) -> None:
        assert len(self._contain(percentiles, lo, hi)) == n

    @pytest.mark.parametrize(
        ("value", "n"),
        [(_JAN_11, 0), ("2026-01-12T00:00:00Z", 1), ("2025-12-31T00:00:00Z", 1)],
    )
    def test_temporal_containment(self, value: str, n: int) -> None:
        assert len(self._contain({"p50": value}, _JAN_1, _JAN_11, classification="temporal")) == n

    @pytest.mark.parametrize(
        ("lo", "hi", "col"),
        [(0, 10, {"redacted": "drop"}), ("x", 10, {}), (0, "x", {}), (0, 10, {"range": "x"})],
    )
    def test_a_range_that_bounds_nothing(self, lo: Any, hi: Any, col: dict[str, Any]) -> None:
        assert self._contain({"p50": 50}, lo, hi, **col) == []


class TestMean:
    def test_a_mean_outside_the_range(self) -> None:
        assert st._check_mean_containment({"mean": 11, "range": {"min": 0, "max": 10}}, _C) == [
            _issue("stats.mean-outside-range", "mean=11 lies outside range [0, 10].", "§2.2.4"),
        ]

    @pytest.mark.parametrize(
        ("mean", "lo", "hi", "n"),
        [
            (10, 0, 10, 0),
            (0, 0, 10, 0),
            (-1, 0, 10, 1),
            (10.000002, 0, 10, 1),
            (-0.0000009, 0, 10, 0),
            (_BIG + 1024, 0, _BIG, 0),
            (_BIG + 2048, 0, _BIG, 1),
        ],
    )
    def test_the_bounds_are_inclusive_to_a_tolerance(
        self,
        mean: float,
        lo: float,
        hi: float,
        n: int,
    ) -> None:
        assert (
            len(st._check_mean_containment({"mean": mean, "range": {"min": lo, "max": hi}}, _C))
            == n
        )

    @pytest.mark.parametrize(
        "col",
        [
            {"mean": 11, "range": {"min": 0, "max": 10}, "redacted": "mask"},
            {"mean": True, "range": {"min": 0, "max": 0}},
            {"mean": 11, "range": {"min": "0", "max": 10}},
            {"mean": 11, "range": {"min": 0, "max": True}},
            {"mean": 11, "range": "x"},
        ],
    )
    def test_a_mean_that_is_not_compared(self, col: dict[str, Any]) -> None:
        assert st._check_mean_containment(col, _C) == []


class TestLengthAndCardinalityOrder:
    @pytest.mark.parametrize(
        "length",
        [{"min": 1, "avg": 5, "max": 3}, {"min": 2, "avg": 1, "max": 3}],
    )
    def test_an_average_outside_its_bounds(self, length: dict[str, int]) -> None:
        assert st._check_length_order({"length": length}, _C) == [
            _issue(
                "stats.length-order-violated",
                f"length.min={length['min']}, length.avg={length['avg']}, length.max={length['max']}; "
                "expected min <= avg <= max.",
                "§2.2.4",
            ),
        ]

    @pytest.mark.parametrize(
        "length",
        [
            {"min": 1, "avg": 1, "max": 1},
            {"min": 1, "avg": True, "max": 0},
            {"min": 1, "avg": 5, "max": "3"},
            "x",
        ],
    )
    def test_an_ordered_or_unreadable_length(self, length: Any) -> None:
        assert st._check_length_order({"length": length}, _C) == []

    def test_a_folded_count_above_the_exact_one(self) -> None:
        assert st._check_normalized_cardinality_order(
            {"normalized_cardinality": 5, "cardinality": 4},
            _C,
        ) == [
            _issue(
                "stats.normalized-cardinality-exceeds-cardinality",
                "normalized_cardinality=5 exceeds cardinality=4; folding case and trimming "
                "whitespace cannot increase distinctness, so the two counts were read in separate "
                "statements.",
                "§2.2.4",
                "warning",
            ),
        ]

    @pytest.mark.parametrize(
        "col",
        [
            {"normalized_cardinality": 4, "cardinality": 4},
            {"normalized_cardinality": 5, "cardinality": 4, "cardinality_method": "approximate"},
            {"normalized_cardinality": True, "cardinality": 0},
            {"normalized_cardinality": 5, "cardinality": True},
            {"normalized_cardinality": 5, "cardinality": "4"},
            {"normalized_cardinality": "5", "cardinality": 4},
        ],
    )
    def test_a_folded_count_that_is_not_compared(self, col: dict[str, Any]) -> None:
        assert st._check_normalized_cardinality_order(col, _C) == []


class TestLooksLikeCandidate:
    def test_a_near_miss_beside_a_verdict_at_the_threshold(self) -> None:
        inferred = {
            "looks_like": "email",
            "looks_like_candidate": "phone",
            "looks_like_candidate_share": 0.95,
        }

        assert st._check_looks_like_candidate({"inferred": inferred}, _C) == [
            _issue(
                "stats.looks-like-candidate-with-verdict",
                "inferred.looks_like_candidate is present alongside inferred.looks_like; the "
                "near-miss applies only where no verdict was reached.",
                "§4.1.3",
            ),
            _issue(
                "stats.looks-like-candidate-at-verdict-threshold",
                "inferred.looks_like_candidate_share=0.95 clears the SPEC 4.1.3 verdict "
                "threshold; a share this high would have been inferred.looks_like instead.",
                "§4.1.3",
            ),
        ]

    @pytest.mark.parametrize(
        ("inferred", "n"),
        [
            ({"looks_like_candidate_share": 0.96}, 1),
            ({"looks_like_candidate_share": 0.94}, 0),
            ({"looks_like_candidate": "phone", "looks_like_candidate_share": True}, 0),
            ({"looks_like_candidate": "phone", "looks_like_candidate_share": "0.99"}, 0),
            ({"looks_like": "email"}, 0),
            ({"looks_like": "email", "looks_like_candidate": "phone"}, 1),
        ],
    )
    def test_each_rule_alone(self, inferred: dict[str, Any], n: int) -> None:
        assert len(st._check_looks_like_candidate({"inferred": inferred}, _C)) == n


class TestPhysicalName:
    def test_a_name_equal_to_the_key(self) -> None:
        assert st._check_physical_name({"physical_name": "c"}, _C, "c") == [
            _issue(
                "stats.physical-name-matches-key",
                "physical_name='c' equals the map key 'c'; omit the field.",
                "§2.2.4",
                "warning",
            ),
        ]

    @pytest.mark.parametrize("name", ["C", 5, None])
    def test_a_name_that_differs(self, name: Any) -> None:
        assert st._check_physical_name({"physical_name": name}, _C, "c") == []


def _packed(*hashes: int) -> str:
    return base64.b64encode(struct.pack(f">{len(hashes)}Q", *hashes)).decode()


class TestSketch:
    def _check(self, values: Any, method: Any = "kmv_md5_lo64", **col: Any) -> list[Issue]:
        return st._check_sketch({"sketch": {"method": method, "values": values}, **col}, _C)

    def test_a_well_formed_sketch(self) -> None:
        assert self._check(_packed(1, 2, 2**64 - 1)) == []

    def test_a_sketch_at_k(self) -> None:
        assert self._check(_packed(*range(1024))) == []

    def test_a_redacted_column_with_an_unknown_method(self) -> None:
        assert self._check(_packed(1), "md5", redacted="mask") == [
            _issue(
                "stats.sketch-on-redacted-column",
                "column declares redacted='mask' and carries a sketch; the digests enumerate the "
                "very values the primitive withheld.",
                "§2.2.14",
            ),
            _issue(
                "stats.sketch-unknown-method",
                "sketch.method='md5' is not a recognized SPEC 2.2.14 method.",
                "§2.2.14",
            ),
        ]

    @pytest.mark.parametrize("values", [5, "not base64!", base64.b64encode(b"1234567").decode()])
    def test_an_undecodable_sketch_stops_there(self, values: Any) -> None:
        assert self._check(values) == [
            _issue(
                "stats.sketch-invalid-encoding",
                "sketch.values is not valid base64 of a multiple of 8 bytes (SPEC 2.2.14's packed "
                "big-endian uint64 array).",
                "§2.2.14",
            ),
        ]

    def test_an_oversized_descending_sketch(self) -> None:
        assert self._check(_packed(*range(1025, 0, -1))) == [
            _issue(
                "stats.sketch-oversized",
                "sketch carries 1025 values, more than k=1024.",
                "§2.2.14",
            ),
            _issue(
                "stats.sketch-not-ascending",
                "sketch.values is not ascending; a KMV sketch's k minimums are unordered or a "
                "byte-order mistake in the producer's own hash.",
                "§2.2.14",
            ),
        ]

    def test_no_sketch(self) -> None:
        assert st._check_sketch({"sketch": "x", "redacted": "mask"}, _C) == []


class TestPercentileEntries:
    def test_values_that_cannot_be_compared_are_dropped(self) -> None:
        percentiles = {"p10": 3, "p50": True, "p90": float("nan"), "p95": float("inf"), "p99": 2}

        assert [
            i.detail for i in st._check_percentiles_order({"percentiles": percentiles}, _C)
        ] == [
            "percentiles.p10=3 exceeds percentiles.p99=2; percentiles must ascend with their keys.",
        ]

    def test_two_spellings_of_one_percent_keep_their_order(self) -> None:
        assert [
            i.detail for i in st._check_percentiles_order({"percentiles": {"p05": 3, "p5": 1}}, _C)
        ] == [
            "percentiles.p05=3 exceeds percentiles.p05=1; percentiles must ascend with their keys.",
        ]


class TestExactBoundaries:
    """A difference of exactly the tolerance is agreement."""

    def test_a_timeline_coverage(self) -> None:
        block = {
            "column": "at",
            "buckets": [{"start": "2026-01-01", "count": 0}],
            "coverage": 1e-06,
        }

        assert (
            st._check_timeline(
                {"timeline": block},
                _P,
                {"at": {"classification": "temporal"}},
                10,
                False,
                10,
            )
            == []
        )

    def test_a_null_pattern_coverage(self) -> None:
        data = {"null_patterns": {"patterns": [{"columns": ["a"], "count": 0}], "coverage": 1e-06}}

        assert st._check_null_patterns(data, _P, {"a": {"null_count": 1}}, 10) == []

    @pytest.mark.parametrize(
        "col",
        [
            {"null_count": 0, "null_rate": 1e-06},
            {"cardinality": 0, "cardinality_ratio": 1e-06},
            {"null_count": 0, "values": _values(0), "values_coverage": 1e-06},
        ],
    )
    def test_count_ratios(self, col: dict[str, Any]) -> None:
        assert _counts(col) == []

    def test_a_mean_exactly_one_tolerance_below_the_range(self) -> None:
        assert (
            st._check_mean_containment({"mean": -1e-06, "range": {"min": 0, "max": 10}}, _C) == []
        )


class TestUnreadableAddresses:
    """An address of the wrong type names nothing, and is never used as a lookup key."""

    def test_a_dependency_naming_lists(self) -> None:
        entries = [{"determinant": ["a"], "dependent": ["b"], "strength": 1}]

        assert (
            st._check_dependencies({"dependencies": entries}, _P, {"a": {}, "b": {}}, 10, False)
            == []
        )

    def test_a_timeline_anchored_on_a_list(self) -> None:
        assert [
            i.code
            for i in st._check_timeline({"timeline": {"column": ["at"]}}, _P, {}, 10, False, 10)
        ] == [
            "stats.timeline-unknown-column",
        ]

    def test_populated_against_an_anchor_named_by_a_list(self) -> None:
        columns = {"b": {"populated": {"from": _JAN_1}}}

        assert st._check_populated({"timeline": {"column": ["at"]}}, _P, columns) == []

    def test_a_null_pattern_naming_a_string_names_no_column(self) -> None:
        data = {"null_patterns": {"patterns": [{"columns": "a", "count": 5}]}}

        assert st._check_null_patterns(data, _P, {"a": {"null_count": 1}}, 10) == []


class TestEveryColumnIsVisited:
    def test_populated(self) -> None:
        columns = {
            "at": {"range": {"min": _JAN_1, "max": _JAN_11}},
            "a": {"populated": {"from": "x"}},
            "b": {"populated": {"from": "1999-01-01T00:00:00Z"}},
        }

        assert [
            i.path for i in st._check_populated({"timeline": {"column": "at"}}, _P, columns)
        ] == [
            f"{_P}::columns.b",
        ]

    def test_populated_with_no_anchor_bounds(self) -> None:
        columns = {
            "at": {},
            "a": {"populated": {"from": "x"}},
            "b": {"populated": {"from": _JAN_1}},
        }

        assert st._check_populated({"timeline": {"column": "at"}}, _P, columns) == []


class TestUnmeasuredUnderAConditionalCell:
    def test_the_cell_removes_only_its_own_fields(self) -> None:
        col = {"classification": "numeric", "redacted": "drop", "unmeasured": ["null_rate"]}

        assert st._check_unmeasured(col, _C, "numeric", 10) == []


class TestFrequencySummaryEdges:
    @pytest.mark.parametrize(
        ("counts", "col", "expected"),
        [
            ((50, 50, 1, 101), {"cardinality": 1}, "dominant_value"),
            ((1, 1, 1, 1), {"cardinality": 1, "null_count": 99}, "dominant_value"),
        ],
    )
    def test_the_verdict(self, counts: tuple[int, ...], col: dict[str, Any], expected: str) -> None:
        assert _frequencies(expected, *counts, **col) == []
        assert len(_frequencies("uniform", *counts, **col)) == 1


class TestPublishedText:
    def test_a_number_orders_by_its_artifact_spelling(self) -> None:
        values = [{"value": 1.5e20, "count": 1}, {"value": 1e20, "count": 1}]

        assert st._check_value_order({"values": values}, _C) == _UNORDERED


class TestEachCheckAddressesItsOwnPath:
    def test_paths(self) -> None:
        data = {
            "row_count": 2,
            "unmeasured": ["grain"],
            "grain": {"keys": []},
            "scope": {"rows_scanned": 2},
            "timeline": {"column": "at"},
            "null_patterns": {"patterns": []},
            "columns": {
                "x": "y",
                "at": {"rows_scanned": 2},
                "c": {
                    "classification": "boolean",
                    "unmeasured": ["mean"],
                    "values": [{"count": 1}],
                    "unrepresentable": [],
                    "inferred": {"looks_like_candidate_share": 0.99},
                    "rows_scanned": 2,
                },
            },
        }

        found = {(i.path, i.code) for i in st.check(data, _P, "s.t")}

        assert found >= {
            (_P, "stats.unmeasured-names-emitted-block"),
            (f"{_P}::scope", "stats.scope-asserts-nothing"),
            (_P, "stats.null-patterns-absent-with-nulls"),
            (_C, "stats.unmeasured-names-unrequired-field"),
            (_C, "stats.redacted-without-marker"),
            (_C, "stats.unrepresentable-empty"),
            (_C, "stats.looks-like-candidate-at-verdict-threshold"),
        }

    def test_populated_is_addressed_at_its_column(self) -> None:
        data = {"columns": {"c": {"populated": {"from": _JAN_1}}}}

        assert [(i.path, i.code) for i in st.check(data, _P, "s.t")] == [
            (_C, "stats.populated-without-timeline"),
        ]

    def test_a_file_with_no_columns_map_still_reads_its_blocks(self) -> None:
        assert [i.code for i in st.check({"null_patterns": {"patterns": []}}, _P, "s.t")] == [
            "stats.null-patterns-absent-with-nulls",
        ]
