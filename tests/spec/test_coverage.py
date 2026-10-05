"""`coverage_share`/`is_incoherent`: the one rule every adapter and the validator go through."""

from __future__ import annotations

import pytest

from dbprint.spec.coverage import coverage_share, enumeration_limit, is_incoherent


class TestCoverageShare:
    """`values_coverage` of 1.0 signals a whole column, so six-place rounding must not reach it."""

    def test_a_long_tail_never_rounds_up_to_one(self) -> None:
        listed = 9_999_996
        non_null = 10_000_000

        assert round(listed / non_null, 6) == 1.0
        assert coverage_share(listed, non_null, exhaustive=False) < 1.0

    def test_an_exhaustive_list_reports_one(self) -> None:
        assert coverage_share(500, 500, exhaustive=True) == 1.0

    def test_an_exhaustive_list_reports_one_even_when_the_raw_ratio_undershoots(self) -> None:
        """Phases A and B can disagree on a live table; a complete list is still exhaustive."""

        assert round(499_636 / 500_000, 6) < 1.0
        assert coverage_share(499_636, 500_000, exhaustive=True) == 1.0

    def test_a_column_with_no_rows_reports_one(self) -> None:
        """An empty list covers everything there is to cover."""

        assert coverage_share(0, 0, exhaustive=True) == 1.0

    def test_a_truncated_list_reports_its_share_unchanged(self) -> None:
        assert coverage_share(20, 100, exhaustive=False) == 0.2

    @pytest.mark.parametrize(
        ("listed", "non_null", "exhaustive"),
        [
            pytest.param(60, 5, True, id="a_12x_overshoot"),
            pytest.param(3, 1, True, id="a_3x_overshoot"),
        ],
    )
    def test_listed_exceeding_non_null_never_publishes_above_one(
        self,
        listed: int,
        non_null: int,
        exhaustive: bool,
    ) -> None:
        """A sampling mismatch between `listed` and `non_null` still keeps the ratio bounded."""

        assert coverage_share(listed, non_null, exhaustive=exhaustive) <= 1.0


class TestIsIncoherent:
    """The numerator/denominator mismatch `coverage_share` bounds away."""

    def test_listed_exceeding_non_null_is_incoherent(self) -> None:
        assert is_incoherent(60, 5) is True

    def test_a_coherent_truncated_list_is_not_incoherent(self) -> None:
        assert is_incoherent(20, 100) is False

    def test_zero_non_null_is_never_incoherent(self) -> None:
        assert is_incoherent(0, 0) is False


class TestTheSharedPhaseBUsesThisFunction:
    """Every adapter's Phase B assembles through `adapters.base`, so a fix here reaches all eight."""

    def test_the_shared_assembly_carries_no_copy(self) -> None:
        from dbprint.adapters.base import coverage_share as shared

        assert shared is coverage_share


def test_the_enumeration_limit_is_the_larger_setting() -> None:
    assert enumeration_limit(50, 20) == 50
    assert enumeration_limit(10, 20) == 20


def test_a_share_is_rounded_to_six_places() -> None:
    assert coverage_share(1, 3, exhaustive=False) == 0.333333


@pytest.mark.parametrize(
    ("listed", "non_null", "incoherent"),
    [(1, 0, False), (2, 1, True), (1, 1, False)],
)
def test_incoherence_needs_rows_to_exceed(listed: int, non_null: int, incoherent: bool) -> None:
    assert is_incoherent(listed, non_null) is incoherent
