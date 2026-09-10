"""The rounding floor and the listed-value transform, across every adapter carrying a copy.

Both helpers are duplicated per adapter, so one adapter drifting from the others is what this
catches - the same hazard `test_exact_int_rounding.py` covers for the exact-integer path.
"""

from __future__ import annotations

import importlib
import importlib.util
from decimal import Decimal
from typing import Any

import pytest

from dbprint.cli.adapter_registry import ADAPTERS


def _helpers_for(adapter: str) -> tuple[Any, Any]:
    module = importlib.import_module(f"dbprint.adapters.{adapter}.stats")

    return module._round_numeric, module._measured_value


_ADAPTERS_WITH_STATS = sorted(
    name
    for name in ADAPTERS
    if importlib.util.find_spec(f"dbprint.adapters.{name}.stats") is not None
)


@pytest.mark.parametrize("adapter", _ADAPTERS_WITH_STATS)
class TestTheFloorKeepsAMeasurementsMagnitude:
    """SPEC 2.2.6: rounding never turns a nonzero measurement into zero."""

    def test_a_value_below_half_a_millionth_keeps_its_magnitude(self, adapter: str) -> None:
        round_numeric, _ = _helpers_for(adapter)

        assert round_numeric(2.7182818284e-07) == pytest.approx(2.71828e-07)

    def test_the_floor_carries_six_significant_figures(self, adapter: str) -> None:
        round_numeric, _ = _helpers_for(adapter)

        assert round_numeric(1.23456789e-09) == pytest.approx(1.23457e-09)

    def test_a_negative_value_keeps_its_sign(self, adapter: str) -> None:
        round_numeric, _ = _helpers_for(adapter)

        assert round_numeric(-4.0e-07) == pytest.approx(-4.0e-07)

    def test_a_real_zero_stays_zero(self, adapter: str) -> None:
        round_numeric, _ = _helpers_for(adapter)

        assert round_numeric(0.0) == 0.0

    def test_an_ordinary_magnitude_still_rounds_to_six_decimals(self, adapter: str) -> None:
        round_numeric, _ = _helpers_for(adapter)

        assert round_numeric(1 / 3) == 0.333333


@pytest.mark.parametrize("adapter", _ADAPTERS_WITH_STATS)
class TestAListedValueIsPublishedAsMeasured:
    """SPEC 2.2.7: a cell value is not a computed statistic, so nothing rounds it."""

    def test_precision_past_the_sixth_decimal_survives(self, adapter: str) -> None:
        _, measured_value = _helpers_for(adapter)

        assert measured_value(31.41592653) == 31.41592653

    def test_two_values_agreeing_to_six_decimals_stay_distinct(self, adapter: str) -> None:
        _, measured_value = _helpers_for(adapter)
        near, nearer = 31.4159265, 31.4159266

        assert measured_value(near) != measured_value(nearer)

    def test_a_decimal_normalizes_to_float(self, adapter: str) -> None:
        """Otherwise it reaches the dumper's own representer, which renders it differently."""

        _, measured_value = _helpers_for(adapter)

        assert measured_value(Decimal("1.2500")) == 1.25

    def test_an_integer_passes_through_unchanged(self, adapter: str) -> None:
        _, measured_value = _helpers_for(adapter)

        assert measured_value(2**53 + 1) == 2**53 + 1

    def test_a_value_that_is_not_a_number_is_returned_as_it_stands(self, adapter: str) -> None:
        _, measured_value = _helpers_for(adapter)

        assert measured_value("alpha") == "alpha"
