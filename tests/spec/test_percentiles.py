"""`spec/percentiles.py` - where a percentile disagreeing with its own bounds is repaired, and where
it is left for the validator to report."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dbprint.spec.percentiles import (
    beyond_float64_precision,
    coherent_percentiles,
)


BASE = 1_700_000_000_000_000_000


class TestBeyondFloat64Precision:
    def test_the_limit_itself_is_exact(self) -> None:
        assert beyond_float64_precision(2**53) is False

    def test_one_past_the_limit_is_not(self) -> None:
        assert beyond_float64_precision(2**53 + 1) is True

    def test_the_negative_side_counts_by_magnitude(self) -> None:
        assert beyond_float64_precision(-(2**53) - 1) is True

    @pytest.mark.parametrize("value", [True, "9007199254740993", None, math.inf, math.nan])
    def test_what_is_not_a_finite_real_is_never_beyond(self, value: object) -> None:
        assert beyond_float64_precision(value) is False


class TestAboveTheLimit:
    """Past 2**53 a float64 percentile and an exact bound sit on two grids, a step apart."""

    def test_a_percentile_below_the_minimum_is_lifted_to_it(self) -> None:
        coherent = coherent_percentiles({"p01": float(BASE) - 256.0}, BASE, BASE + 1_000_000)

        assert coherent["p01"] == BASE
        assert isinstance(coherent["p01"], float)

    def test_a_percentile_above_the_maximum_takes_the_nearest_float_inside(self) -> None:
        coherent = coherent_percentiles({"p99": float(BASE + 1_256)}, BASE, BASE + 1_000)

        assert coherent["p99"] == float(BASE + 768)  # noqa: RUF069 - integers convert to float exactly
        assert isinstance(coherent["p99"], float)

    def test_a_descent_between_keys_is_flattened(self) -> None:
        given = {"p25": float(BASE + 512), "p50": float(BASE + 256)}

        coherent = coherent_percentiles(given, BASE, BASE + 1_000_000)

        assert coherent == {"p25": float(BASE + 512), "p50": float(BASE + 512)}

    def test_keys_are_ordered_by_percent_not_by_spelling(self) -> None:
        given = {"p100": float(BASE + 256), "p99": float(BASE + 512)}

        coherent = coherent_percentiles(given, BASE, BASE + 1_000_000)

        assert coherent["p100"] == float(BASE + 512)  # noqa: RUF069 - integers convert to float exactly

    def test_an_int_percentile_is_already_exact_and_never_rewritten(self) -> None:
        given = {"p01": BASE - 5}

        assert coherent_percentiles(given, BASE, BASE + 1_000) == given

    def test_a_single_value_with_no_float_inside_falls_back_to_the_rounded_bound(self) -> None:
        odd = 2**53 + 1

        coherent = coherent_percentiles({"p50": float(2**53) - 2.0}, odd, odd)

        assert coherent["p50"] == float(odd)  # noqa: RUF069 - integers convert to float exactly


class TestBelowTheLimit:
    """Where float64 is exact, a percentile outside its bounds is a real defect, published as is."""

    def test_a_percentile_below_the_minimum_is_left(self) -> None:
        given = {"p01": -5.0, "p50": 100.0}

        assert coherent_percentiles(given, 95, 105) == given

    def test_a_descent_is_left(self) -> None:
        given = {"p25": 101.0, "p50": 99.0}

        assert coherent_percentiles(given, 95, 105) == given

    def test_the_limit_itself_keeps_the_gate_closed(self) -> None:
        given = {"p01": float(2**53) - 1.0}

        assert coherent_percentiles(given, 2**53, 2**53 + 10) == given

    def test_one_past_the_limit_opens_it(self) -> None:
        coherent = coherent_percentiles({"p01": float(2**53)}, 2**53 + 1, 2**53 + 10)

        assert coherent["p01"] >= 2**53 + 1


class TestAStraddlingRange:
    """The gate is per comparison: a wide range's far end never licenses its near end."""

    def test_a_percentile_far_below_a_minimum_near_zero_is_left(self) -> None:
        coherent = coherent_percentiles({"p01": -500.0}, 0, BASE)

        assert coherent["p01"] == -500.0  # noqa: RUF069 - the expected value is an exact literal

    def test_a_percentile_above_the_far_maximum_is_still_clamped(self) -> None:
        coherent = coherent_percentiles({"p99": float(BASE) + 256.0}, 0, BASE)

        assert coherent["p99"] <= BASE


class TestWhatPassesThrough:
    def test_a_temporal_percentile_is_untouched(self) -> None:
        given = {"p01": "2026-01-01T00:00:00Z"}

        assert coherent_percentiles(given, "2026-01-02T00:00:00Z", "2026-02-01T00:00:00Z") == given

    def test_the_input_is_not_mutated(self) -> None:
        given = {"p01": float(BASE) - 256.0}

        coherent_percentiles(given, BASE, BASE + 1_000_000)

        assert given == {"p01": float(BASE) - 256.0}


class TestARealDefectIsLeftForTheValidator:
    """Only a gap of a few float64 steps is a grid artifact; a wider one is a measurement."""

    def test_a_percentile_far_under_a_huge_minimum_is_published_as_measured(self) -> None:
        assert coherent_percentiles({"p01": 0.0}, BASE, BASE + 1_000_000) == {"p01": 0.0}

    def test_a_percentile_far_above_a_huge_maximum_is_published_as_measured(self) -> None:
        coherent = coherent_percentiles({"p99": 2.0 * BASE}, BASE, BASE + 1_000_000)

        assert coherent == {"p99": 2.0 * BASE}

    def test_a_real_descent_between_large_neighbours_is_published_as_measured(self) -> None:
        given = {"p25": float(BASE), "p50": 1.0e18}

        assert coherent_percentiles(given, BASE // 2, BASE * 2) == given

    @pytest.mark.parametrize(
        ("steps", "repaired"),
        [(4, True), (5, False)],
    )
    def test_the_tolerance_edge(self, steps: int, repaired: bool) -> None:
        low = BASE + 1_024
        value = float(low) - steps * math.ulp(float(low))

        coherent = coherent_percentiles({"p01": value}, low, low + 1_000_000)

        assert (coherent["p01"] >= low) is repaired


_EXACT = st.floats(min_value=-(2**53), max_value=2**53) | st.integers(-(2**53), 2**53)
_KEYS = st.sampled_from(["p01", "p05", "p25", "p50", "p75", "p95", "p99"])


@st.composite
def _just_outside(draw: st.DrawFn) -> tuple[dict[str, float], float, float]:
    low = draw(st.floats(min_value=-(2**52), max_value=2**52))
    high = draw(st.floats(min_value=low, max_value=2**52))
    percentiles = {}

    for key in draw(st.lists(_KEYS, min_size=1, max_size=4, unique=True)):
        value = low if draw(st.booleans()) else high

        for _ in range(draw(st.integers(1, 4))):
            value = math.nextafter(value, -math.inf if value == low else math.inf)

        percentiles[key] = value

    return percentiles, low, high


class TestCoherentPercentilesProperties:
    @given(st.dictionaries(_KEYS, _EXACT), _EXACT, _EXACT)
    def test_values_inside_the_exact_range_pass_through_unchanged(
        self,
        percentiles: dict[str, float | int],
        low: float,
        high: float,
    ) -> None:
        assert coherent_percentiles(percentiles, low, high) == percentiles

    @given(_just_outside())
    def test_a_near_miss_inside_the_exact_range_is_left_for_the_validator(
        self,
        case: tuple[dict[str, float], float, float],
    ) -> None:
        percentiles, low, high = case

        assert coherent_percentiles(percentiles, low, high) == percentiles


def test_a_repair_onto_an_exact_bound_keeps_the_bound() -> None:
    high = 2**60
    above = math.nextafter(float(high), math.inf)

    assert coherent_percentiles({"p99": above}, 0, high) == {"p99": float(high)}


def test_a_repair_steps_inside_onto_a_bound_that_is_itself_a_float() -> None:
    low, high = 2**60 + 1, 2**60 + 256

    assert coherent_percentiles({"p01": float(2**60)}, low, high) == {"p01": float(high)}
