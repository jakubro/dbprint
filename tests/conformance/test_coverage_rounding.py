"""The validator recomputes `values_coverage` through the producer's own `coverage_share`."""

from __future__ import annotations

from typing import Any

from dbprint.conformance import statistics
from dbprint.spec.coverage import coverage_share


PATH = "public/t/statistics.yaml"
FQN = "public.t"


def _codes(column: dict[str, Any], row_count: int) -> set[str]:
    payload = {
        "format_version": 1,
        "table": FQN,
        "type": "table",
        "profiled_at": "2026-01-01T00:00:00Z",
        "row_count": row_count,
        "row_count_method": "exact",
        "columns": {"c": column},
    }

    # Not error-filtered: stats.values-sum-mismatch is a warning, and absence needs no filter.
    return {i.code for i in statistics.check(payload, PATH, FQN)}


def _truncated_column(listed: int, non_null: int) -> dict[str, Any]:
    """A categorical column whose truncated list sums to `listed` of `non_null`."""

    return {
        "sql_type": "TEXT",
        "nullable": False,
        "null_count": 0,
        "classification": "categorical",
        "cardinality": 21,
        "cardinality_ratio": round(21 / non_null, 6),
        "cardinality_method": "exact",
        "values": [{"value": "a", "count": listed}],
        "values_coverage": coverage_share(listed, non_null, exhaustive=False),
        "distribution": "dominant_value",
    }


class TestABoundaryTruncatedListValidatesClean:
    """A list one short of a large denominator rounds to 1.0 raw, but the producer clamps it."""

    def test_off_by_one_at_the_rounding_boundary(self) -> None:
        assert coverage_share(24_999_999, 25_000_000, exhaustive=False) < 1.0

    def test_off_by_four_at_the_rounding_boundary(self) -> None:
        assert coverage_share(24_999_996, 25_000_000, exhaustive=False) < 1.0

    def test_an_ordinary_truncated_tail_is_unaffected(self) -> None:
        column = _truncated_column(800, 1000)

        assert column["values_coverage"] == 0.8
        assert "stats.values-coverage-mismatch" not in _codes(column, 1000)


class TestTheRuleStillCatchesWhatItExistsToCatch:
    """Widening a tolerance until nothing fails is not a fix - these must still fail."""

    def test_a_deliberately_wrong_coverage_fails(self) -> None:
        column = _truncated_column(24_999_999, 25_000_000)
        column["values_coverage"] = 0.5

        assert "stats.values-coverage-mismatch" in _codes(column, 25_000_000)

    def test_an_exhaustive_list_summing_to_non_null_passes(self) -> None:
        column = _truncated_column(1000, 1000)
        column["values_coverage"] = 1.0

        assert "stats.values-coverage-mismatch" not in _codes(column, 1000)
        assert "stats.values-sum-mismatch" not in _codes(column, 1000)

    def test_the_same_list_with_one_count_altered_fails(self) -> None:
        """`coverage_share` forces 1.0 for any exhaustive claim, so a published 1.0 always
        agrees and `stats.values-coverage-mismatch` cannot see this defect. The two codes
        checking the claim directly do: short of `cardinality`, and not summing to non_null.
        """

        column = _truncated_column(999, 1000)
        column["values_coverage"] = 1.0

        codes = _codes(column, 1000)

        assert "stats.values-sum-mismatch" in codes
        assert "stats.values-list-short-of-cardinality" in codes
        assert "stats.values-coverage-mismatch" not in codes


def _exhaustive_column(listed: int, non_null: int, cardinality: int) -> dict[str, Any]:
    """Every distinct value listed; `listed` and `non_null` are reads that may disagree."""

    return {
        "sql_type": "TEXT",
        "nullable": False,
        "null_count": 0,
        "classification": "categorical",
        "cardinality": cardinality,
        "cardinality_ratio": round(cardinality / non_null, 6),
        "cardinality_method": "exact",
        "values": [{"value": "a", "count": listed}],
        "values_coverage": coverage_share(listed, non_null, exhaustive=True),
        "distribution": "dominant_value",
    }


class TestAnExhaustiveListPublishesOneRegardlessOfTheRawRatio:
    """`exhaustive` decides `values_coverage`, so a complete list never under-claims."""

    def test_the_published_coverage_is_one(self) -> None:
        column = _exhaustive_column(listed=499_636, non_null=500_000, cardinality=1)

        assert column["values_coverage"] == 1.0

    def test_an_exhaustive_list_publishes_exactly_one(self) -> None:
        """Exhaustive is 1.0 whatever the raw quotient of the two reads comes to."""

        assert coverage_share(499_636, 500_000, exhaustive=True) == 1.0

    def test_the_drift_that_produced_the_undershoot_still_surfaces_as_a_warning(self) -> None:
        """Publishing 1.0 does not hide the phase disagreement, only reclassifies it."""

        column = _exhaustive_column(listed=499_636, non_null=500_000, cardinality=1)
        issues = statistics.check(
            {
                "format_version": 1,
                "table": FQN,
                "type": "table",
                "profiled_at": "2026-01-01T00:00:00Z",
                "row_count": 500_000,
                "row_count_method": "exact",
                "columns": {"c": column},
            },
            PATH,
            FQN,
        )
        match = next(i for i in issues if i.code == "stats.values-sum-mismatch")

        assert match.severity == "warning"


class TestAnIncoherentTruncatedListIsReportedNotAbsorbed:
    """Listed exceeding non_null clamps to the same coverage an honest tail can also reach."""

    def test_listed_exceeding_non_null_is_reported(self) -> None:
        codes = _codes(_truncated_column(1_000_001, 1_000_000), 1_000_000)

        assert "stats.values-sum-mismatch" in codes
        assert "stats.values-coverage-mismatch" not in codes

    def test_an_honest_tail_at_the_same_published_coverage_stays_silent(self) -> None:
        assert "stats.values-sum-mismatch" not in _codes(
            _truncated_column(999_999, 1_000_000),
            1_000_000,
        )


def _census_payload(listed: int, rows_scanned: int, coverage: float) -> dict[str, Any]:
    """A one-column print whose null census lists `listed` of `rows_scanned` rows."""

    return {
        "format_version": 1,
        "table": FQN,
        "type": "table",
        "profiled_at": "2026-01-01T00:00:00Z",
        "row_count": rows_scanned,
        "row_count_method": "exact",
        "null_patterns": {
            "coverage": coverage,
            "patterns": [{"columns": ["c"], "count": listed}],
        },
        "columns": {
            "c": {
                "sql_type": "TEXT",
                "nullable": True,
                "null_count": rows_scanned,
                "null_rate": 1.0,
                "cardinality": 0,
                "cardinality_ratio": 0.0,
                "cardinality_method": "exact",
                "classification": "categorical",
                "values": [],
                "values_coverage": 1.0,
                "distribution": "uniform",
            },
        },
    }


class TestTheNullCensusIsRecomputedThroughTheProducersRule:
    """A truncated census whose omitted tail rounds away still publishes below 1.0.

    `coverage_share` clamps it to `TRUNCATED_CLAMP`; a validator recomputing the raw ratio
    would read 1.0 and reject the producer's own output on the large tables the census is for.
    """

    def test_a_tail_that_rounds_away_is_not_reported_as_a_mismatch(self) -> None:
        # 3_999_999 / 4_000_000 = 0.99999975, which rounds to 1.000000 at six decimals - the
        # producer's clamp is what keeps a merely-truncated census from publishing 1.0 outright.
        listed, rows_scanned = 3_999_999, 4_000_000
        published = 0.999999

        codes = {
            i.code
            for i in statistics.check(
                _census_payload(listed, rows_scanned, published),
                PATH,
                FQN,
            )
        }

        assert "stats.null-patterns-coverage-mismatch" not in codes

    def test_a_genuinely_wrong_coverage_is_still_reported(self) -> None:
        codes = {i.code for i in statistics.check(_census_payload(500, 1_000, 0.9), PATH, FQN)}

        assert "stats.null-patterns-coverage-mismatch" in codes
