"""`compute_null_rate` (spec/classification.py) - the shared rule every adapter writes
`null_rate` through and the conformance validator recomputes, guarded against a copy.
"""

from __future__ import annotations

from dbprint.spec.classification import compute_null_rate


class TestComputeNullRate:
    def test_rounds_to_six_places(self) -> None:
        assert compute_null_rate(1, 3) == 0.333333

    def test_zero_nulls_is_zero(self) -> None:
        assert compute_null_rate(0, 1000) == 0.0

    def test_no_rows_scanned_is_zero(self) -> None:
        """SPEC 2.2.2: `0` when `rows_scanned == 0`, not a division error."""

        assert compute_null_rate(0, 0) == 0.0

    def test_a_nonzero_null_count_never_rounds_down_to_zero(self) -> None:
        assert round(1 / 10_000_000, 6) == 0.0
        assert compute_null_rate(1, 10_000_000) == 0.000001

    def test_a_nonzero_non_null_count_never_rounds_up_to_one(self) -> None:
        assert round(9_999_999 / 10_000_000, 6) == 1.0
        assert compute_null_rate(9_999_999, 10_000_000) == 0.999999

    def test_an_all_null_column_still_reports_the_true_sentinel(self) -> None:
        """The ceiling is gated on a nonzero non-null count, never on the rounded value."""

        assert compute_null_rate(1000, 1000) == 1.0


class TestAllThreeAdaptersShareOneFunction:
    """De-triplication is the point - a fix to one must be a fix to all three."""

    def test_no_adapter_carries_its_own_copy(self) -> None:
        from dbprint.adapters.mysql.stats import compute_null_rate as mysql_compute_null_rate
        from dbprint.adapters.postgres.stats import compute_null_rate as postgres_compute_null_rate
        from dbprint.adapters.snowflake.stats import (
            compute_null_rate as snowflake_compute_null_rate,
        )

        assert postgres_compute_null_rate is compute_null_rate
        assert mysql_compute_null_rate is compute_null_rate
        assert snowflake_compute_null_rate is compute_null_rate
