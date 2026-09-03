"""`_round_numeric`'s exact-integer path, across every adapter that carries a copy of it.

The helper is duplicated per adapter, so one adapter drifting from the others is what this catches.
"""

from __future__ import annotations

import importlib
import importlib.util
import math
from decimal import Decimal
from typing import Any

import pytest

from dbprint.cli.adapter_registry import ADAPTERS


def _round_numeric_for(adapter: str) -> Any:
    module = importlib.import_module(f"dbprint.adapters.{adapter}.stats")

    return module._round_numeric


_ADAPTERS_WITH_STATS = sorted(
    name
    for name in ADAPTERS
    if importlib.util.find_spec(f"dbprint.adapters.{name}.stats") is not None
)


@pytest.mark.parametrize("adapter", _ADAPTERS_WITH_STATS)
class TestExactIntegerTotals:
    """SPEC 2.2.6: an integral total publishes exact, since float64 loses precision above 2**53."""

    def test_an_integral_decimal_beyond_float64_precision_stays_exact(self, adapter: str) -> None:
        round_numeric = _round_numeric_for(adapter)
        big = Decimal(2**53 + 1)

        assert round_numeric(big, exact_int=True) == 2**53 + 1
        assert isinstance(round_numeric(big, exact_int=True), int)

    def test_a_non_integral_decimal_still_rounds(self, adapter: str) -> None:
        round_numeric = _round_numeric_for(adapter)

        assert round_numeric(Decimal("1.5"), exact_int=True) == 1.5

    def test_without_the_flag_an_integral_decimal_is_left_rate_valued(self, adapter: str) -> None:
        round_numeric = _round_numeric_for(adapter)
        out = round_numeric(Decimal(7))

        assert out == 7.0
        assert isinstance(out, float)


@pytest.mark.parametrize("adapter", _ADAPTERS_WITH_STATS)
@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
class TestNonFiniteCellsDoNotFailTheirTable:
    """Postgres and Redshift `numeric` hold `NaN` and the infinities, and `SUM` returns one - so
    testing integrality before finiteness would end the whole table over one cell.
    """

    def test_a_non_finite_decimal_returns_a_value_rather_than_raising(
        self,
        adapter: str,
        literal: str,
    ) -> None:
        round_numeric = _round_numeric_for(adapter)
        out = round_numeric(Decimal(literal), exact_int=True)

        assert isinstance(out, float)
        assert math.isnan(out) or math.isinf(out)
