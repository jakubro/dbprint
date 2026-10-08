"""`round_statistic`'s exact-integer path (SPEC 2.2.6)."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from dbprint.engine.yaml_dumper import dump_yaml
from dbprint.spec.rounding import round_statistic


class TestExactIntegerTotals:
    """SPEC 2.2.6: an integral total publishes exact, since float64 loses precision above 2**53."""

    def test_an_integral_decimal_beyond_float64_precision_stays_exact(self) -> None:
        big = Decimal(2**53 + 1)

        assert round_statistic(big, exact_int=True) == 2**53 + 1
        assert isinstance(round_statistic(big, exact_int=True), int)

    def test_a_non_integral_decimal_still_rounds(self) -> None:
        assert round_statistic(Decimal("1.5"), exact_int=True) == 1.5  # noqa: RUF069 - the expected value is an exact literal

    def test_without_the_flag_an_integral_decimal_is_left_rate_valued(self) -> None:
        out = round_statistic(Decimal(7))

        assert isinstance(out, Decimal)
        assert dump_yaml({"mean": out}).strip() == "mean: 7.0"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
class TestNonFiniteCellsDoNotFailTheirTable:
    """Postgres and Redshift `numeric` hold `NaN` and the infinities, and `SUM` returns one - so
    testing integrality before finiteness would end the whole table over one cell.
    """

    def test_a_non_finite_decimal_returns_a_value_rather_than_raising(self, literal: str) -> None:
        out = round_statistic(Decimal(literal), exact_int=True)

        assert isinstance(out, float)
        assert math.isnan(out) or math.isinf(out)
