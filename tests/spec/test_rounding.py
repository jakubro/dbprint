"""SPEC 2.2.6 rounding, over generated measurements: stable once applied, and never zero from nonzero."""

from __future__ import annotations

import datetime
import math
import uuid
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dbprint.spec.rounding import (
    UnrepresentableValue,
    is_rounded,
    measured_text,
    measured_value,
    number_text,
    round_statistic,
)


class TestRoundStatisticProperties:
    @given(st.floats(allow_nan=False, allow_infinity=False))
    def test_a_rounded_float_is_already_rounded(self, value: float) -> None:
        assert is_rounded(round_statistic(value))

    @given(st.decimals(allow_nan=False, allow_infinity=False))
    def test_rounding_a_rounded_decimal_changes_nothing(self, value: Decimal) -> None:
        once = round_statistic(value)

        assert round_statistic(once) == once

    @given(st.floats(allow_nan=False, allow_infinity=False).filter(bool))
    def test_a_nonzero_float_never_rounds_to_zero(self, value: float) -> None:
        assert round_statistic(value) != 0

    @given(st.decimals(allow_nan=False, allow_infinity=False).filter(bool))
    def test_a_nonzero_decimal_never_rounds_to_zero(self, value: Decimal) -> None:
        assert round_statistic(value) != 0


@pytest.mark.parametrize(
    ("value", "exact_int", "expected"),
    [
        (None, False, None),
        (7, False, 7),
        (1.23456789, False, 1.234568),
        (-1.2345678e-07, False, -1.23457e-07),
        ("n/a", False, "n/a"),
        (Decimal("1.2345678"), False, Decimal("1.234568")),
        (Decimal("0.0000005"), False, Decimal("5.00000E-7")),
        (Decimal("1.2345678E-7"), False, Decimal("1.23457E-7")),
        (
            Decimal("123456789012345678901234567890.1234567"),
            False,
            Decimal("123456789012345678901234567890.123457"),
        ),
    ],
)
def test_round_statistic_rounds_to_six_places_and_floors_at_six_figures(
    value: object,
    exact_int: bool,
    expected: object,
) -> None:
    got = round_statistic(value, exact_int=exact_int)

    assert (type(got), got) == (type(expected), expected)
    assert str(got) == str(expected)


def test_an_integral_decimal_is_an_int_only_for_a_count() -> None:
    assert type(round_statistic(Decimal(5), exact_int=True)) is int
    assert type(round_statistic(Decimal(5))) is Decimal
    assert type(round_statistic(Decimal("2.5"), exact_int=True)) is Decimal


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (True, True),
        (3, 3),
        (1.5, 1.5),
        ("x", "x"),
        (Decimal("12.3400"), Decimal("12.34")),
        (Decimal("0.50"), Decimal("0.5")),
        (Decimal("5.00"), 5),
        (datetime.date(2024, 1, 2), "2024-01-02"),
        (datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC), "2024-01-02T03:04:05Z"),
        (uuid.UUID(int=1), "00000000-0000-0000-0000-000000000001"),
    ],
)
def test_measured_value_publishes_each_cell_exactly(value: object, expected: object) -> None:
    got = measured_value(value, "values")

    assert (type(got), str(got)) == (type(expected), str(expected))


def test_a_non_finite_decimal_is_published_as_a_float() -> None:
    nan = measured_value(Decimal("NaN"))

    assert isinstance(nan, float) and math.isnan(nan)
    assert measured_value(Decimal("-Infinity")) == -math.inf


class _Int(int):
    pass


@pytest.mark.parametrize(
    ("value", "type_name"),
    [
        (b"x", "bytes"),
        (Fraction(1, 3), "fractions.Fraction"),
        (_Int(3), "tests.spec.test_rounding._Int"),
    ],
)
def test_measured_value_refuses_a_type_it_cannot_spell(value: object, type_name: str) -> None:
    with pytest.raises(UnrepresentableValue) as refused:
        measured_value(value, "range.min")

    assert refused.value.field == "range.min"
    assert str(refused.value).startswith(f"range.min is {type_name}, ")


def test_measured_text_spells_a_temporal_and_refuses_a_number() -> None:
    assert measured_text(datetime.date(2024, 5, 6), "timeline.bucket") == "2024-05-06"

    with pytest.raises(UnrepresentableValue, match=r"^timeline\.bucket is int, "):
        measured_text(4, "timeline.bucket")

    with pytest.raises(UnrepresentableValue, match=r"^timeline\.bucket is bytes, "):
        measured_text(b"4", "timeline.bucket")


def test_an_unrepresentable_value_names_where_it_was_found() -> None:
    suffix = "which has no artifact spelling (producer defect; please report)"

    assert str(UnrepresentableValue(b"x", table="a.t", column="c", field="values")) == (
        f"table 'a.t', column 'c': values is bytes, {suffix}"
    )
    assert str(UnrepresentableValue(b"x", column="c")) == f"column 'c': a value is bytes, {suffix}"
    assert str(UnrepresentableValue(Fraction(1, 2))) == f"a value is fractions.Fraction, {suffix}"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(Decimal("2.50"), "2.5"), (Decimal(10), "10.0"), (1e22, "10000000000000000000000.0")],
)
def test_number_text_is_positional_with_a_decimal_point(
    value: Decimal | float,
    expected: str,
) -> None:
    assert number_text(value) == expected
