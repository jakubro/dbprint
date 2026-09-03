"""Epoch-unit detection: the bounds rule, the per-value rule, and their shared windows."""

from __future__ import annotations

import importlib.resources
import json
from decimal import Decimal
from typing import get_args

import pytest

from dbprint.spec.epoch import EpochUnit, bounds_epoch_unit, sample_epoch_unit
from dbprint.spec.looks_like import MATCH_THRESHOLD


# isdigit()-true, isdecimal()-false: a superscript, a subscript, and a circled digit.
_HOSTILE_DIGITS = ("\u00b9", "\u2081", "\u2460")


class TestEnumAgreement:
    """The Python `Literal` and the JSON Schema enum are hand-maintained twins."""

    def test_the_literal_and_the_schema_enum_carry_the_same_values(self) -> None:
        text = importlib.resources.files("dbprint.spec.v1").joinpath("statistics.schema.json")
        schema = json.loads(text.read_text())
        schema_values = set(schema["$defs"]["EpochUnit"]["enum"])

        assert set(get_args(EpochUnit)) == schema_values


class TestBoundsRule:
    """`numeric`: both `range.min` and `range.max`, no sample, no threshold."""

    def test_a_seconds_window_column_is_detected(self) -> None:
        assert bounds_epoch_unit(1704067200, 1786492800) == "seconds"

    def test_a_milliseconds_window_column_is_detected(self) -> None:
        assert bounds_epoch_unit(1704067200000, 1786492800000) == "milliseconds"

    def test_an_id_sequence_seeded_at_the_floor_is_a_named_positive(self) -> None:
        """The accepted false positive: both bounds happen to fall inside the window."""

        assert bounds_epoch_unit(1000000042, 1000998765) == "seconds"

    def test_a_32_bit_id_past_the_ceiling_is_not_detected(self) -> None:
        """Arithmetic, not a special case: the max exceeds the seconds window's ceiling."""

        assert bounds_epoch_unit(1073741824, 2147000000) is None

    def test_byte_sizes_are_not_detected(self) -> None:
        """The min sits below the floor - what requiring both bounds buys."""

        assert bounds_epoch_unit(0, 1800000000) is None

    def test_a_nanp_phone_number_is_not_detected(self) -> None:
        """A 10-digit NANP number starts at 2e9, above the seconds window's ceiling."""

        assert bounds_epoch_unit(2015550100, 9995550199) is None

    def test_ordinary_counts_are_not_detected(self) -> None:
        assert bounds_epoch_unit(0, 5000) is None

    def test_epoch_microseconds_are_a_stated_gap(self) -> None:
        assert bounds_epoch_unit(1704067200000000, 1786492800000000) is None

    def test_a_non_integral_bound_is_not_detected(self) -> None:
        assert bounds_epoch_unit(1704067200.5, 1786492800) is None

    def test_a_whole_float_bound_is_detected(self) -> None:
        """A driver may return a whole `float`/`Decimal` for a numeric column."""

        assert bounds_epoch_unit(1704067200.0, 1786492800.0) == "seconds"
        assert bounds_epoch_unit(Decimal(1704067200), Decimal(1786492800)) == "seconds"

    def test_a_boolean_bound_is_not_detected(self) -> None:
        """`bool` is an `int` subclass; excluded since `numeric` never classifies boolean."""

        assert bounds_epoch_unit(True, True) is None

    def test_a_non_numeric_bound_is_not_detected(self) -> None:
        assert bounds_epoch_unit("not a number", 1786492800) is None

    def test_both_windows_boundaries_are_inclusive(self) -> None:
        assert bounds_epoch_unit(1_000_000_000, 2_000_000_000) == "seconds"
        assert bounds_epoch_unit(1_000_000_000_000, 2_000_000_000_000) == "milliseconds"

    def test_just_outside_the_seconds_ceiling_is_not_detected(self) -> None:
        assert bounds_epoch_unit(1_000_000_000, 2_000_000_001) is None

    def test_just_outside_the_seconds_floor_is_not_detected(self) -> None:
        assert bounds_epoch_unit(999_999_999, 2_000_000_000) is None


class TestPerValueRule:
    """The three SPEC 4.1.5 sampled classifications: reuses `looks_like`'s own threshold."""

    def test_a_seconds_text_column_is_detected(self) -> None:
        values = [str(1704067200 + i) for i in range(30)]

        assert sample_epoch_unit(values) == "seconds"

    def test_a_milliseconds_text_column_is_detected(self) -> None:
        values = [str(1704067200000 + i) for i in range(30)]

        assert sample_epoch_unit(values) == "milliseconds"

    def test_a_mixed_column_below_threshold_is_not_detected(self) -> None:
        """90% epoch strings, 10% a placeholder - 0.90 cannot clear 0.95."""

        values = [str(1704067200 + i) for i in range(27)] + ["unknown"] * 3

        assert sample_epoch_unit(values) is None

    def test_noise_within_tolerance_still_detects(self) -> None:
        values = [str(1704067200 + i) for i in range(29)] + ["unknown"]

        assert len(values) == 30
        assert 29 / 30 >= MATCH_THRESHOLD
        assert sample_epoch_unit(values) == "seconds"

    def test_an_empty_sample_is_not_detected(self) -> None:
        assert sample_epoch_unit([]) is None

    def test_non_integral_values_are_not_detected(self) -> None:
        assert sample_epoch_unit(["not-a-number"] * 30) is None

    def test_native_int_values_are_detected(self) -> None:
        """A native `BIGINT` epoch column's categorical sample, unstringified."""

        values: list[object] = [1704067200 + i for i in range(30)]

        assert sample_epoch_unit(values) == "seconds"

    def test_an_ordinary_digit_string_column_is_not_detected(self) -> None:
        assert sample_epoch_unit([str(i) for i in range(30)]) is None


class TestHostileCharacters:
    """isdigit()-true, isdecimal()-false codepoints must not reach `int()` and raise."""

    @pytest.mark.parametrize("digit", _HOSTILE_DIGITS)
    def test_a_hostile_digit_in_the_sample_does_not_raise(self, digit: str) -> None:
        values = [f"19{digit}1949"] * 30

        assert sample_epoch_unit(values) is None

    @pytest.mark.parametrize("digit", _HOSTILE_DIGITS)
    def test_a_hostile_digit_in_a_bound_does_not_raise(self, digit: str) -> None:
        assert bounds_epoch_unit(f"19{digit}1949", 1786492800) is None


class TestTheTwoRulesAreDisjointByConstruction:
    """SPEC 4.5: a column reached by both rules cannot disagree with itself.

    `numeric` carries no sample and the other four no `range` (SPEC 2.2.3), so no column
    gets both verdicts.
    """

    def test_bounds_and_sample_verdicts_agree_on_the_same_instants(self) -> None:
        lo, hi = 1704067200, 1786492800
        values = [str(lo), str(hi)] * 15

        assert bounds_epoch_unit(lo, hi) == sample_epoch_unit(values) == "seconds"
