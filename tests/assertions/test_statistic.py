"""Statistic assertion evaluator tests per ASSERTIONS.md 2.

`_stats_for()` loads seedbank.accession's own real `statistics.yaml` from the shipped print,
so every predicate is checked against real recorded values: `catalogue_url` is a candidate key
whose `looks_like` is `url`, `provenance_country` a categorical with an exhaustive census.
TestRedactedFreshness needs a redacted TEMPORAL column, which the shipped print has none of,
so it keeps its own hand-built fixture.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from dbprint.assertions import AssertionSet, TablePredicates, evaluate_statistic_assertions


_ACCESSION_STATISTICS_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs/format/v1/examples/production/prints/production/seedbank/accession/statistics.yaml"
)


def _stats_for() -> dict[str, dict]:
    return {"seedbank.accession": yaml.safe_load(_ACCESSION_STATISTICS_PATH.read_text())}


def _set(tables: dict[str, TablePredicates] | None = None) -> AssertionSet:
    return AssertionSet(tables=tables or {})


class TestRowCount:
    def test_passes_within_min(self) -> None:
        aset = _set(
            {"seedbank.accession": TablePredicates("seedbank.accession", row_count={"min": 500})},
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert issues == []

    def test_fails_below_min(self) -> None:
        aset = _set(
            {"seedbank.accession": TablePredicates("seedbank.accession", row_count={"min": 5000})},
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.row-count-mismatch"


class TestNullRate:
    def test_scalar_pass(self) -> None:
        aset = _set(
            {
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
                    columns={"catalogue_url": {"null_rate": 0.0}},
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", _stats_for()) == []

    def test_scalar_fail(self) -> None:
        aset = _set(
            {
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
                    columns={"provenance_country": {"classification": "categorical"}},
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", _stats_for()) == []

    def test_enum_fail(self) -> None:
        aset = _set(
            {
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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


class TestLooksLike:
    def test_match(self) -> None:
        aset = _set(
            {
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
                    columns={"catalogue_url": {"looks_like": "url"}},
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", _stats_for()) == []

    def test_mismatch(self) -> None:
        aset = _set(
            {
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
                    columns={"catalogue_url": {"candidate_key": True}},
                ),
            },
        )
        assert evaluate_statistic_assertions(aset, "primary", _stats_for()) == []

    def test_mismatch(self) -> None:
        aset = _set(
            {
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
                    columns={"catalogue_url": {"candidate_key": False}},
                ),
            },
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.candidate-key-mismatch"


class TestUnknownTable:
    def test_warning(self) -> None:
        """seedbank.taxon is real, just outside this run's profiled set (only accession is)."""

        aset = _set(
            {"seedbank.taxon": TablePredicates("seedbank.taxon", row_count={"min": 1})},
        )
        issues = evaluate_statistic_assertions(aset, "primary", _stats_for())
        assert len(issues) == 1
        assert issues[0].code == "assertion.unknown-table"
        assert issues[0].severity == "warning"


class TestUnknownColumn:
    def test_warning(self) -> None:
        aset = _set(
            {
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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
                "seedbank.accession": TablePredicates(
                    "seedbank.accession",
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
