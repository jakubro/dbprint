"""The rounding floor (SPEC 2.2.6) and the listed-value transform (SPEC 2.2.7).

Both are one shared rule, tested once; `test_exact_numerics.py` pins every adapter to it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from dbprint.spec.rounding import measured_value, round_statistic


class TestTheFloorKeepsAMeasurementsMagnitude:
    """SPEC 2.2.6: rounding never turns a nonzero measurement into zero."""

    def test_a_value_below_half_a_millionth_keeps_its_magnitude(self) -> None:
        assert round_statistic(2.7182818284e-07) == pytest.approx(2.71828e-07)

    def test_the_floor_carries_six_significant_figures(self) -> None:
        assert round_statistic(1.23456789e-09) == pytest.approx(1.23457e-09)

    def test_a_negative_value_keeps_its_sign(self) -> None:
        assert round_statistic(-4.0e-07) == pytest.approx(-4.0e-07)

    def test_a_real_zero_stays_zero(self) -> None:
        assert round_statistic(0.0) == 0.0

    def test_an_ordinary_magnitude_still_rounds_to_six_decimals(self) -> None:
        assert round_statistic(1 / 3) == 0.333333


class TestAListedValueIsPublishedAsMeasured:
    """SPEC 2.2.7: a cell value is not a computed statistic, so nothing rounds it."""

    def test_precision_past_the_sixth_decimal_survives(self) -> None:
        assert measured_value(31.41592653) == 31.41592653

    def test_two_values_agreeing_to_six_decimals_stay_distinct(self) -> None:
        assert measured_value(31.4159265) != measured_value(31.4159266)

    def test_a_non_integral_decimal_stays_exact(self) -> None:
        published = measured_value(Decimal("1.2500"))

        assert (type(published), published) == (Decimal, Decimal("1.25"))

    def test_an_integer_passes_through_unchanged(self) -> None:
        assert measured_value(2**53 + 1) == 2**53 + 1

    def test_a_value_that_is_not_a_number_is_returned_as_it_stands(self) -> None:
        assert measured_value("alpha") == "alpha"
