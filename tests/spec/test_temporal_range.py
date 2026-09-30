"""A rendered temporal literal's year classifies and orders it, sign, era and infinity included."""

from __future__ import annotations

import math

import pytest

from dbprint.spec.temporal_range import is_representable, leading_year


@pytest.mark.parametrize(
    ("rendered", "representable"),
    [
        ("2024-03-01", True),
        ("0001-01-01T00:00:00", True),
        ("10000-01-01", False),
        ("-0005-01-01", False),
        ("0044-03-15 BC", False),
        ("infinity", False),
    ],
)
def test_the_representable_range_is_years_one_to_9999(rendered: str, representable: bool) -> None:
    assert is_representable(rendered) is representable


@pytest.mark.parametrize(
    ("rendered", "year"),
    [
        ("2024-03-01", 2024),
        ("294276-01-01", 294276),
        ("-0005-01-01", -5),
        ("0001-01-01 BC", 0),
        ("infinity", math.inf),
        ("-infinity", -math.inf),
    ],
)
def test_the_leading_year_orders_every_rendering(rendered: str, year: float) -> None:
    found = leading_year(rendered)

    assert found is not None
    assert found.year == year


def test_text_naming_no_year_has_none() -> None:
    assert leading_year("next tuesday") is None
    assert leading_year(2024) is None


def test_a_bc_marker_is_read_past_trailing_space() -> None:
    assert is_representable("0044-03-15 BC ") is False
    year = leading_year("0044-03-15 BC ")

    assert year is not None and year.year == -43


def test_the_last_representable_year_is_9999() -> None:
    assert is_representable("9999-12-31") is True
    assert is_representable("0001-01-01") is True
