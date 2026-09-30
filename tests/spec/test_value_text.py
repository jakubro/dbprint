"""`scalar_text` spells every value as the artifact file writes it and the validator reads it."""

from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from dbprint.conformance.statistics import _published_text
from dbprint.conformance.yaml_utils import load_yaml
from dbprint.engine.yaml_dumper import ArtifactDumper, dump_yaml
from dbprint.spec.value_text import scalar_text, spell_number, spell_percent, value_order_key


SAMPLES: list[tuple[Any, str]] = [
    ("NULL", "NULL"),
    ("1e3", "1e3"),
    ("", ""),
    ("B", "B"),
    ("é", "é"),
    (True, "true"),
    (False, "false"),
    (-10, "-10"),
    (12345678901234567890, "12345678901234567890"),
    (1e-07, "0.0000001"),
    (1e16, "10000000000000000.0"),
    (-5e-05, "-0.00005"),
    (0.1, "0.1"),
    (2.5, "2.5"),
    (float("inf"), ".inf"),
    (Decimal("12.50"), "12.5"),
    (datetime.timedelta(hours=9), "09:00:00"),
    (datetime.timedelta(hours=10), "10:00:00"),
    (datetime.timedelta(days=1), "24:00:00"),
    (datetime.timedelta(hours=-1), "-01:00:00"),
    (datetime.timedelta(days=1, milliseconds=500), "24:00:00.500000"),
    (datetime.datetime(2024, 1, 9, 9, 30), "2024-01-09T09:30:00"),  # noqa: DTZ001 - naive
    (datetime.datetime(2024, 1, 9, 9, 30, tzinfo=datetime.UTC), "2024-01-09T09:30:00Z"),
    (datetime.date(2024, 1, 9), "2024-01-09"),
    (datetime.time(9, 30, 15), "09:30:15"),
    (uuid.UUID(int=10), "00000000-0000-0000-0000-00000000000a"),
]

NOT_A_VALUE = {type(None), bytes, bytearray, memoryview, list, tuple, dict, set, frozenset}


@pytest.mark.parametrize(("value", "text"), SAMPLES, ids=[repr(v) for v, _ in SAMPLES])
def test_the_file_and_the_validator_read_the_same_text(
    tmp_path: Path,
    value: Any,
    text: str,
) -> None:
    dumped = dump_yaml({"v": value})
    node = yaml.compose(dumped)
    assert isinstance(node, yaml.MappingNode)
    path = tmp_path / "v.yaml"
    path.write_text(dumped, encoding="utf-8")

    assert scalar_text(value) == text
    assert node.value[0][1].value == text
    assert _published_text(load_yaml(path)["v"]) == text


def test_every_representer_type_is_sampled_or_declared_not_a_value() -> None:
    sampled = {type(v) for v, _ in SAMPLES}
    unspelled = {
        t
        for t in ArtifactDumper.yaml_representers
        if t is not None and t not in NOT_A_VALUE and not any(issubclass(s, t) for s in sampled)
    }

    assert unspelled == set()


def test_an_unknown_type_raises() -> None:
    with pytest.raises(TypeError, match="no artifact spelling"):
        scalar_text(object())


def test_ties_order_on_code_point_after_count() -> None:
    entries = [(5, "0.5"), (5, "0.0000001"), (7, "z"), (5, "B"), (5, "b")]

    assert sorted(entries, key=lambda e: value_order_key(*e)) == [
        (7, "z"),
        (5, "0.0000001"),
        (5, "0.5"),
        (5, "B"),
        (5, "b"),
    ]


@pytest.mark.parametrize(
    "number",
    [1e16, 1e-9, 2.0**64, 18446744073709548000.0, 4.89613e-08, 0.1, -5e-05],
)
def test_a_float_statistic_reads_back_exactly_and_never_in_exponent_form(number: float) -> None:
    text = spell_number(number)

    assert float(text) == number
    assert "e" not in text.lower()


def test_an_int_is_its_digits_at_any_width() -> None:
    assert spell_number(2**64 - 1) == "18446744073709551615"


@pytest.mark.parametrize("value", [True, "1", None])
def test_a_non_number_is_not_spelled_as_one(value: Any) -> None:
    with pytest.raises(TypeError):
        spell_number(value)


@pytest.mark.parametrize(
    ("ratio", "signed", "text"),
    [
        (0.9996, False, "99.96%"),
        (0.9999, False, "99.99%"),
        (0.0004, False, "0.04%"),
        (1.0, False, "100%"),
        (1 / 3, False, "33.3%"),
        (0, False, "0%"),
        (0.25, True, "+25%"),
        (-0.0001, True, "-0.01%"),
        (1.5, True, "+150%"),
        (0.0000001, False, "0.00001%"),
        (0.9496, False, "95%"),
        (1e30, True, "+100000000000000000000000000000000%"),
    ],
)
def test_a_partial_share_never_reads_as_0_or_100_percent(
    ratio: float,
    signed: bool,
    text: str,
) -> None:
    assert spell_percent(ratio, signed=signed) == text


class TestSpellNumberProperties:
    @given(st.floats(allow_nan=False, allow_infinity=False))
    def test_a_float_is_positional_and_parses_back_exactly(self, value: float) -> None:
        text = spell_number(value)

        assert "e" not in text.lower()
        assert float(text) == value

    @given(st.decimals(allow_nan=False, allow_infinity=False))
    def test_a_decimal_is_positional_and_parses_back_exactly(self, value: Decimal) -> None:
        text = spell_number(value)

        assert "e" not in text.lower()
        assert Decimal(text) == value


class TestSpellPercentProperties:
    @given(st.floats(min_value=0, max_value=1, exclude_min=True, exclude_max=True))
    def test_a_partial_float_share_never_reads_as_none_or_all(self, ratio: float) -> None:
        assert spell_percent(ratio) not in {"0%", "100%"}

    @given(st.decimals(min_value=0, max_value=1, allow_nan=False, allow_infinity=False))
    def test_a_partial_decimal_share_never_reads_as_none_or_all(self, ratio: Decimal) -> None:
        if 0 < ratio < 1:
            assert spell_percent(ratio) not in {"0%", "100%"}


@pytest.mark.parametrize("spell", [spell_number, spell_percent])
@pytest.mark.parametrize(("value", "type_name"), [("1", "str"), (True, "bool"), (None, "NoneType")])
def test_a_non_number_is_refused_by_type(
    spell: Callable[[Any], str],
    value: object,
    type_name: str,
) -> None:
    with pytest.raises(TypeError, match=f"^not a number: {type_name}$"):
        spell(value)


@pytest.mark.parametrize(
    ("ratio", "signed", "expected"),
    [
        (Decimal("0.000149"), False, "0.01%"),
        (-0.0, False, "0%"),
        (0.0, True, "0%"),
        (0.005, True, "+0.5%"),
        (-0.005, True, "-0.5%"),
        (1, False, "100%"),
        (float("inf"), False, ".inf%"),
        (float("nan"), False, ".nan%"),
    ],
)
def test_spell_percent_edges(ratio: float | Decimal, signed: bool, expected: str) -> None:
    assert spell_percent(ratio, signed=signed) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (float("nan"), ".nan"),
        (float("inf"), ".inf"),
        (float("-inf"), "-.inf"),
        (datetime.timedelta(0), "00:00:00"),
        (datetime.timedelta(minutes=1), "00:01:00"),
        (datetime.timedelta(seconds=-1), "-00:00:01"),
    ],
)
def test_scalar_text_of_non_finite_numbers_and_durations(value: object, expected: str) -> None:
    assert scalar_text(value) == expected
