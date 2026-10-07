"""Statistic assertion evaluator tests per ASSERTIONS.md 2.

`_stats_for()` loads arboretum.seedbank.accession's own real `statistics.yaml` from the shipped print,
so every predicate is checked against real recorded values: `catalogue_url` is a candidate key
whose `looks_like` is `url`, `provenance_country` a categorical with an exhaustive census.
TestRedactedFreshness needs a redacted TEMPORAL column, which the shipped print has none of,
so it keeps its own hand-built fixture.
"""

from __future__ import annotations

import yaml

from dbprint.assertions import AssertionSet, TablePredicates, evaluate_statistic_assertions
from dbprint.conformance.issue import Issue
from tests._scripts import REPO_ROOT


_ACCESSION_STATISTICS_PATH = (
    REPO_ROOT
    / "docs/format/v1/examples/production/prints/production/arboretum/seedbank/accession/statistics.yaml"
)


def _stats_for() -> dict[str, dict]:
    return {"arboretum.seedbank.accession": yaml.safe_load(_ACCESSION_STATISTICS_PATH.read_text())}


def _set(tables: dict[str, TablePredicates] | None = None) -> AssertionSet:
    return AssertionSet(tables=tables or {})


class TestRowCount:
    def test_passes_within_min(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    row_count={"min": 500},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert issues == []

    def test_fails_below_min(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    row_count={"min": 5000},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.row-count-mismatch"


class TestNullRate:
    def test_scalar_pass(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"catalogue_url": {"null_rate": 0.0}},
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", _stats_for()) == []

    def test_scalar_fail(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"catalogue_url": {"null_rate": 0.5}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.null-rate-mismatch"


class TestClassification:
    def test_enum_pass(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"provenance_country": {"classification": "categorical"}},
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", _stats_for()) == []

    def test_enum_fail(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"provenance_country": {"classification": "text"}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.classification-mismatch"


class TestAcceptedValues:
    def test_subset_passes(self) -> None:
        """The real census is 10 countries; a set naming all ten plus one more still covers it."""

        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={
                        "provenance_country": {
                            "accepted_values": [
                                "AU",
                                "CA",
                                "DE",
                                "FR",
                                "GB",
                                "IE",
                                "NL",
                                "NZ",
                                "US",
                                "ZA",
                                "XX",
                            ],
                        },
                    },
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", _stats_for()) == []

    def test_extra_values_fail(self) -> None:
        """Omitting one real country code (ZA) leaves the real census not a subset."""

        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={
                        "provenance_country": {
                            "accepted_values": [
                                "AU",
                                "CA",
                                "DE",
                                "FR",
                                "GB",
                                "IE",
                                "NL",
                                "NZ",
                                "US",
                            ],
                        },
                    },
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.accepted-values-violated"
        assert "ZA" in issues[0].detail


class TestAcceptedValuesUnderScope:
    """A list complete over the rows scanned is not the table's domain (SPEC 2.2.8)."""

    def test_a_scoped_list_is_inapplicable_and_names_the_scope(self) -> None:
        stats = _stats_for()
        stats["arboretum.seedbank.accession"]["scope"] = {"rows_scanned": 40, "sample": 0.1}
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"provenance_country": {"accepted_values": ["AU"]}},
                ),
            },
        )

        [issue] = evaluate_statistic_assertions(aset, "primary", stats)

        assert (issue.code, issue.severity) == ("assertion.inapplicable-stat", "warning")
        assert "over the rows scanned only (Scanned: 40" in issue.detail


class TestLooksLike:
    def test_match(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"catalogue_url": {"looks_like": "url"}},
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", _stats_for()) == []

    def test_mismatch(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"catalogue_url": {"looks_like": "email"}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.looks-like-mismatch"


class TestCandidateKey:
    def test_match_true(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"catalogue_url": {"candidate_key": True}},
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", _stats_for()) == []

    def test_mismatch(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"catalogue_url": {"candidate_key": False}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.candidate-key-mismatch"


class TestUnknownTable:
    def test_warning(self) -> None:
        """arboretum.seedbank.taxon is real, just outside this run's profiled set (only accession is)."""

        aset = _set(
            {
                "arboretum.seedbank.taxon": TablePredicates(
                    "arboretum.seedbank.taxon",
                    row_count={"min": 1},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.unknown-table"
        assert issues[0].severity == "warning"


class TestUnknownColumn:
    def test_warning(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"missing_col": {"null_rate": 0.0}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert any(i.code == "assertion.unknown-column" for i in issues)


class TestUnknownStat:
    def test_error(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"catalogue_url": {"made_up_stat": 1}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.unknown-stat"


class TestInapplicableStat:
    def test_warning_when_stat_absent(self) -> None:
        # catalogue_url is text; no `range` field. Predicate on range.min -> inapplicable.
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"catalogue_url": {"range.min": {"min": 0}}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.inapplicable-stat"
        assert issues[0].severity == "warning"


def _legacy_stats_for() -> dict[str, dict]:
    """A hand-built table kept only for TestRedactedFreshness below.

    That class needs a redacted TEMPORAL column for SPEC 2.2.9's freshness refusal, and every
    redacted column in the shipped print is categorical or text.
    """

    return {
        "public.curator": {
            "table": "public.curator",
            "row_count": 1000,
            "columns": {
                "id": {
                    "sql_type": "uuid",
                    "nullable": False,
                    "null_count": 0,
                    "null_rate": 0.0,
                    "cardinality": 1000,
                    "cardinality_ratio": 1.0,
                    "classification": "text",
                    "inferred": {"candidate_key": True, "looks_like": "uuid"},
                },
                "rank": {
                    "sql_type": "varchar",
                    "nullable": False,
                    "null_count": 0,
                    "null_rate": 0.0,
                    "cardinality": 3,
                    "cardinality_ratio": 0.003,
                    "classification": "categorical",
                    "values": [
                        {"value": "bronze", "count": 800},
                        {"value": "silver", "count": 150},
                        {"value": "gold", "count": 50},
                    ],
                    "values_coverage": 1.0,
                    "distribution": "imbalanced",
                },
            },
        },
    }


def _stats_with_redacted_dob(primitive: str) -> dict[str, dict]:
    stats = _legacy_stats_for()
    stats["public.curator"]["columns"]["date_of_birth"] = {
        "sql_type": "date",
        "nullable": True,
        "null_count": 0,
        "null_rate": 0.0,
        "cardinality": 1000,
        "cardinality_ratio": 1.0,
        "classification": "temporal",
        "redacted": primitive,
        # true age 91 floors to 90 (SPEC 2.2.9), the boundary a {min: 91} predicate straddles.
        "freshness": {"max_age_days": 90, "classification": "dormant"},
    }

    return stats


class TestRedactedFreshness:
    def test_max_age_days_refuses_under_mask(self) -> None:
        aset = _set(
            {
                "public.curator": TablePredicates(
                    "public.curator",
                    columns={"date_of_birth": {"freshness.max_age_days": {"min": 91}}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_with_redacted_dob("mask"))
        assert len(issues) == 1
        assert issues[0].code == "assertion.redacted-stat"
        assert issues[0].severity == "warning"

    def test_max_age_days_refuses_under_drop(self) -> None:
        """`drop` still emits `freshness`, so the refusal must still fire."""

        aset = _set(
            {
                "public.curator": TablePredicates(
                    "public.curator",
                    columns={"date_of_birth": {"freshness.max_age_days": {"min": 91}}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_with_redacted_dob("drop"))
        assert len(issues) == 1
        assert issues[0].code == "assertion.redacted-stat"

    def test_classification_still_evaluates_under_mask(self) -> None:
        aset = _set(
            {
                "public.curator": TablePredicates(
                    "public.curator",
                    columns={"date_of_birth": {"freshness.classification": "dormant"}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_with_redacted_dob("mask"))
        assert issues == []

    def test_max_age_days_unaffected_on_unredacted_column(self) -> None:
        stats = _legacy_stats_for()
        stats["public.curator"]["columns"]["date_of_birth"] = {
            "sql_type": "date",
            "nullable": True,
            "null_count": 0,
            "null_rate": 0.0,
            "cardinality": 1000,
            "cardinality_ratio": 1.0,
            "classification": "temporal",
            "freshness": {"max_age_days": 91, "classification": "dormant"},
        }
        aset = _set(
            {
                "public.curator": TablePredicates(
                    "public.curator",
                    columns={"date_of_birth": {"freshness.max_age_days": {"min": 91}}},
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", stats) == []


class TestDeterministicOrdering:
    def test_issues_sorted_by_path(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={
                        "catalogue_url": {"null_rate": 0.5, "classification": "text"},
                        "provenance_country": {"distribution": "dominant_value"},
                    },
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        paths = [i.path for i in issues]
        assert paths == sorted(paths)


class TestMultipleColumnPredicatesCompose:
    def test_all_must_pass(self) -> None:
        # catalogue_url is a real candidate key; both predicates check its unique shape.
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={
                        "catalogue_url": {
                            "null_rate": 0.0,
                            "cardinality_ratio": {"min": 0.99},
                        },
                    },
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", _stats_for()) == []

    def test_one_failure_surfaces(self) -> None:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={
                        "catalogue_url": {
                            "null_rate": 0.0,
                            "cardinality_ratio": {"min": 99.0},  # impossible
                        },
                    },
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        codes = {i.code for i in issues}
        assert "assertion.cardinality-ratio-mismatch" in codes


class TestTemporalPercentile:
    """A temporal percentile compares as a date, never as its spelling."""

    def _issues(self, bound: dict) -> list:
        aset = _set(
            {
                "arboretum.seedbank.accession": TablePredicates(
                    "arboretum.seedbank.accession",
                    columns={"collected_on": {"percentiles.p50": bound}},
                ),
            },
        )

        return evaluate_statistic_assertions(aset, "primary", _stats_for())

    def test_a_bound_the_median_meets_passes(self) -> None:
        assert self._issues({"min": "2021-06-04T00:00:00Z"}) == []

    def test_a_bound_the_median_misses_fails(self) -> None:
        assert len(self._issues({"min": "2021-06-05"})) == 1


_NUMBER = {
    "sql_type": "integer",
    "nullable": False,
    "null_count": 0,
    "null_rate": 0.0,
    "classification": "numeric",
    "cardinality": 10,
    "cardinality_ratio": 1.0,
    "cardinality_method": "exact",
    "range": {"min": 1, "max": 10},
    "percentiles": {"p50": 5},
}
_NEAR_KEY = {
    "sql_type": "text",
    "nullable": False,
    "null_count": 0,
    "null_rate": 0.0,
    "classification": "text",
    "cardinality": 9,
    "cardinality_ratio": 0.9,
    "cardinality_method": "exact",
    "values": [{"value": "a", "count": 1}],
    "values_coverage": 0.1,
    "distribution": "uniform",
}
_HAND_STATS = {
    "s.t": {
        "row_count": 10,
        "columns": {"n": _NUMBER, "k": _NEAR_KEY, "r": {**_NUMBER, "redacted": "mask"}},
    },
    "s.uncounted": {"columns": {"n": _NUMBER}},
}


def _issues(tables: dict[str, TablePredicates]) -> list[Issue]:
    return evaluate_statistic_assertions(AssertionSet(tables=tables), "conn", _HAND_STATS)


def _columns(**columns: dict) -> dict[str, TablePredicates]:
    return {"s.t": TablePredicates("s.t", columns=dict(columns))}


class TestEveryIssueCarriesItsPathCodeSeveritySpecRefAndDetail:
    """ASSERTIONS.md 5: a finding is addressed by its path and cites the section behind it."""

    def test_a_table_the_print_lacks(self) -> None:
        assert _issues({"s.gone": TablePredicates("s.gone", row_count={"min": 1})}) == [
            Issue(
                path="assertions.conn.tables.s.gone",
                code="assertion.unknown-table",
                severity="warning",
                detail="table 's.gone' not in manifest; skipping predicates",
                spec_ref="ASSERTIONS.md §1.4",
            ),
        ]

    def test_a_row_count_the_file_does_not_carry(self) -> None:
        assert _issues({"s.uncounted": TablePredicates("s.uncounted", row_count={"min": 1})}) == [
            Issue(
                path="assertions.conn.tables.s.uncounted.row_count",
                code="assertion.inapplicable-stat",
                severity="warning",
                detail="row_count is not_applicable: not emitted for this file",
                spec_ref="ASSERTIONS.md §2.6",
            ),
        ]

    def test_a_row_count_outside_its_bound(self) -> None:
        assert _issues({"s.t": TablePredicates("s.t", row_count={"min": 20})}) == [
            Issue(
                path="assertions.conn.tables.s.t.row_count",
                code="assertion.row-count-mismatch",
                severity="error",
                detail="actual 10 < min 20",
                spec_ref="ASSERTIONS.md §2",
            ),
        ]

    def test_a_row_count_predicate_of_no_known_shape(self) -> None:
        assert _issues({"s.t": TablePredicates("s.t", row_count={"above": 1})}) == [
            Issue(
                path="assertions.conn.tables.s.t.row_count",
                code="assertion.malformed-predicate",
                severity="error",
                detail="range predicate accepts only min and/or max keys",
                spec_ref="ASSERTIONS.md §2",
            ),
        ]

    def test_an_unknown_column_does_not_stop_the_columns_after_it(self) -> None:
        assert _issues(_columns(absent={"null_count": 0}, n={"null_count": 5})) == [
            Issue(
                path="assertions.conn.tables.s.t.columns.absent",
                code="assertion.unknown-column",
                severity="warning",
                detail="column 'absent' not in 's.t' statistics",
                spec_ref="ASSERTIONS.md §1.4",
            ),
            Issue(
                path="assertions.conn.tables.s.t.columns.n.null_count",
                code="assertion.null-count-mismatch",
                severity="error",
                detail="expected 5, actual 0",
                spec_ref="ASSERTIONS.md §2",
            ),
        ]

    def test_a_value_bearing_stat_on_a_redacted_column(self) -> None:
        assert _issues(_columns(r={"range.min": {"min": 0}})) == [
            Issue(
                path="assertions.conn.tables.s.t.columns.r.range.min",
                code="assertion.redacted-stat",
                severity="warning",
                detail=(
                    "'range.min' cannot be evaluated: this column is redacted (mask), so its "
                    "emitted values are not its real ones"
                ),
                spec_ref="§2.2.9",
            ),
        ]

    def test_a_stat_outside_the_vocabulary(self) -> None:
        assert _issues(_columns(n={"spread": 1})) == [
            Issue(
                path="assertions.conn.tables.s.t.columns.n.spread",
                code="assertion.unknown-stat",
                severity="error",
                detail="stat 'spread' not in §2.4 vocabulary",
                spec_ref="ASSERTIONS.md §2.4",
            ),
        ]

    def test_a_column_predicate_of_the_wrong_shape(self) -> None:
        assert _issues(_columns(n={"accepted_values": "1"})) == [
            Issue(
                path="assertions.conn.tables.s.t.columns.n.accepted_values",
                code="assertion.malformed-predicate",
                severity="error",
                detail="accepted_values requires a list",
                spec_ref="ASSERTIONS.md §2.1",
            ),
        ]

    def test_a_stat_the_classification_does_not_carry(self) -> None:
        assert _issues(_columns(n={"looks_like": "url"})) == [
            Issue(
                path="assertions.conn.tables.s.t.columns.n.looks_like",
                code="assertion.inapplicable-stat",
                severity="warning",
                detail="stat 'looks_like' not emitted for column 'n': not carried by numeric",
                spec_ref="ASSERTIONS.md §2.6",
            ),
        ]

    def test_a_failed_verdict_names_the_measurement_it_rests_on(self) -> None:
        assert _issues(_columns(k={"candidate_key": True})) == [
            Issue(
                path="assertions.conn.tables.s.t.columns.k.candidate_key",
                code="assertion.candidate-key-mismatch",
                severity="error",
                detail="expected True, actual False (cardinality_ratio 0.9)",
                spec_ref="ASSERTIONS.md §2",
            ),
        ]

    def test_a_percentile_mismatch_has_its_own_code(self) -> None:
        assert _issues(_columns(n={"percentiles.p50": 3})) == [
            Issue(
                path="assertions.conn.tables.s.t.columns.n.percentiles.p50",
                code="assertion.percentile-mismatch",
                severity="error",
                detail="expected 3, actual 5",
                spec_ref="ASSERTIONS.md §2",
            ),
        ]

    def test_a_bound_on_a_range_endpoint(self) -> None:
        assert _issues(_columns(n={"range.min": {"min": 5}})) == [
            Issue(
                path="assertions.conn.tables.s.t.columns.n.range.min",
                code="assertion.range-out-of-bounds",
                severity="error",
                detail="actual 1 < min 5",
                spec_ref="ASSERTIONS.md §2",
            ),
        ]


class TestEvaluationContinuesPastEachFinding:
    def test_a_table_the_print_lacks_does_not_stop_the_tables_after_it(self) -> None:
        issues = _issues(
            {
                "s.gone": TablePredicates("s.gone", row_count={"min": 1}),
                "s.t": TablePredicates("s.t", row_count={"min": 20}),
            },
        )

        assert [issue.code for issue in issues] == [
            "assertion.unknown-table",
            "assertion.row-count-mismatch",
        ]


class TestEachStatIsParsedByItsOwnForm:
    def test_accepted_values_passes_on_a_superset_of_the_listed_values(self) -> None:
        column = {
            **_NEAR_KEY,
            "classification": "categorical",
            "cardinality": 1,
            "cardinality_ratio": 0.1,
            "values": [{"value": "a", "count": 10}],
            "values_coverage": 1.0,
        }
        stats = {"s.t": {"row_count": 10, "columns": {"c": column}}}
        tables = {"s.t": TablePredicates("s.t", columns={"c": {"accepted_values": ["a", "b"]}})}

        assert evaluate_statistic_assertions(AssertionSet(tables=tables), "conn", stats) == []

    def test_a_numeric_string_verdict_is_never_expected_on_a_numeric_type(self) -> None:
        column = {**_NUMBER, "classification": "categorical", "inferred": {"looks_like": "phone"}}
        stats = {"s.t": {"row_count": 10, "columns": {"c": column}}}
        tables = {"s.t": TablePredicates("s.t", columns={"c": {"looks_like": "numeric_string"}})}

        assert evaluate_statistic_assertions(AssertionSet(tables=tables), "conn", stats) == [
            Issue(
                path="assertions.conn.tables.s.t.columns.c.looks_like",
                code="assertion.inapplicable-stat",
                severity="warning",
                detail="looks_like 'numeric_string' is never published on a numeric SQL type",
                spec_ref="ASSERTIONS.md §2.6",
            ),
        ]


def test_every_failure_code_names_an_assertable_stat() -> None:
    """A code for a stat the vocabulary dropped would never be raised; a missing one raises KeyError."""

    from dbprint.assertions.issue import _FAILURE_CODES
    from dbprint.spec.predicate import ASSERTABLE_STATS

    assert set(_FAILURE_CODES) == ASSERTABLE_STATS
