"""Every scalar an adapter can return needs a case: `SafeDumper` rejects unrepresented types."""

from __future__ import annotations

import datetime
import decimal
import uuid

import pytest
import yaml
import yaml.representer

from dbprint.engine.yaml_dumper import dump_yaml, spell_inline


class TestDriverScalars:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (
                uuid.UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
                "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            ),
            (decimal.Decimal("1.500"), "1.500"),
            (datetime.date(2026, 7, 31), "2026-07-31"),
            (datetime.datetime(2026, 7, 31, 15, 0), "2026-07-31T15:00:00"),  # noqa: DTZ001 - naive
            (
                datetime.datetime(2026, 7, 31, 15, 0, tzinfo=datetime.UTC),
                "2026-07-31T15:00:00Z",
            ),
            (datetime.time(15, 0), "15:00:00"),
            (datetime.time(15, 0, 30, 500000), "15:00:30.500000"),
            (datetime.timedelta(0), "00:00:00"),
            (datetime.timedelta(hours=15), "15:00:00"),
            (datetime.timedelta(hours=15, minutes=30, seconds=45), "15:30:45"),
        ],
        ids=[
            "uuid",
            "decimal-keeps-trailing-zeros",
            "date",
            "naive-datetime",
            "utc-datetime-gets-z",
            "time",
            "time-with-microseconds",
            "zero-timedelta",
            "whole-hour-timedelta",
            "hms-timedelta",
        ],
    )
    def test_scalar_round_trips_as_a_yaml_string(self, value: object, expected: str) -> None:
        assert yaml.safe_load(dump_yaml({"v": value}))["v"] == expected

    @pytest.mark.parametrize(
        "value",
        [datetime.time(15, 0), datetime.timedelta(hours=15), decimal.Decimal("1.5")],
        ids=["time", "timedelta", "decimal"],
    )
    def test_scalar_survives_the_mapping_key_position(self, value: object) -> None:
        """A `values` map puts the driver value in the key slot, resolved before the value slot."""

        assert yaml.safe_load(dump_yaml({value: 3})).popitem()[1] == 3


class TestUnrepresentableTypes:
    def test_an_unknown_object_still_raises(self) -> None:
        """A blanket `str()` would let malformed values into an artifact."""

        with pytest.raises(yaml.representer.RepresenterError):
            dump_yaml({"v": object()})


def _emitted_scalar(text: str) -> str:
    """The raw emitted `v: ...` value, byte-exact - a round trip proves nothing here."""

    return dump_yaml({"v": text}).removeprefix("v: ").rstrip("\n")


class TestYaml12QuotedScalars:
    @pytest.mark.parametrize(
        "value",
        [
            "112e334455667788",
            "00112233445566e6",
            "99887766554433e5",
            "0e0",
            "+1e5",
            "1E5",
            "1e5",
            ".inf",
            "-.inf",
            ".nan",
            "0x1f",
            "0o17",
            "00123",
        ],
        ids=[
            "digest-shaped-float",
            "trailing-e6",
            "trailing-e5",
            "zero-e-zero",
            "leading-plus",
            "uppercase-e",
            "bare-exponent",
            "inf",
            "negative-inf",
            "nan",
            "hex-literal",
            "octal-literal",
            "leading-zeros",
        ],
    )
    def test_float_and_int_shaped_strings_are_quoted(self, value: str) -> None:
        assert _emitted_scalar(value) == f"'{value}'"
        assert yaml.safe_load(dump_yaml({"v": value}))["v"] == value

    @pytest.mark.parametrize("value", ["yes", "no", "on", "off"])
    def test_yaml11_boolean_words_stay_quoted(self, value: str) -> None:
        """These match no 1.2 bool/int/float grammar this representer checks; PyYAML's own
        resolver quotes them anyway, and a bare `no` would read back as `False`.
        """

        assert _emitted_scalar(value) == f"'{value}'"

    @pytest.mark.parametrize(
        "value",
        ["hello world", "10.0.12.30", "abc", "seedbank.accession", "a1b2c3"],
    )
    def test_ordinary_strings_stay_unquoted(self, value: str) -> None:
        assert _emitted_scalar(value) == value

    @pytest.mark.parametrize(
        "value",
        ["-0o17", "+0o17", "+.nan", "-.nan"],
        ids=["neg-octal", "pos-octal", "pos-nan", "neg-nan"],
    )
    def test_signed_octal_and_nan_stay_unquoted(self, value: str) -> None:
        """YAML v1.2 gives 0o int and .nan no sign, so a signed form is unambiguous."""

        assert _emitted_scalar(value) == value

    @pytest.mark.parametrize("value", ["-0x1f", "+0x1f"], ids=["neg-hex", "pos-hex"])
    def test_signed_hex_stays_quoted_via_pyyamls_own_resolver(self, value: str) -> None:
        """YAML v1.2 gives 0x int no sign, so this representer does not force the quote;
        PyYAML's 1.1 resolver does, and correctly - its loader reads `-0x1f` back as `-31`.
        """

        assert _emitted_scalar(value) == f"'{value}'"

    def test_genuine_float_field_is_unaffected(self) -> None:
        """`_represent_float` owns Python floats; this representer never sees one."""

        assert dump_yaml({"v": 0.000123}).strip() == "v: 0.000123"


class TestSpellInline:
    """One value, flow style, one line - the form a reader would type, not Python's repr."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (True, "true"),
            (False, "false"),
            (None, "null"),
            (100, "100"),
            (["a", "b"], "[a, b]"),
            ({"max": 0.01}, "{max: 0.01}"),
            ("0", "'0'"),
            ("plain", "plain"),
            (decimal.Decimal("1.500"), "'1.500'"),
        ],
        ids=[
            "true",
            "false",
            "null",
            "int",
            "list",
            "dict",
            "numeric-looking-string-quoted",
            "ordinary-string-unquoted",
            "decimal-keeps-trailing-zeros-and-quotes-to-round-trip-as-a-string",
        ],
    )
    def test_shapes(self, value: object, expected: str) -> None:
        assert spell_inline(value) == expected

    def test_carries_no_document_end_marker(self) -> None:
        """A bare scalar document normally gets PyYAML's own `...` end-of-document line."""

        assert "\n" not in spell_inline(True)
        assert "..." not in spell_inline(True)
