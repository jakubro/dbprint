"""Every scalar an adapter can return needs a case: `SafeDumper` rejects unrepresented types."""

from __future__ import annotations

import datetime
import decimal
import unicodedata
import uuid

import pytest
import yaml
from hypothesis import example, given
from hypothesis import strategies as st

from dbprint.engine.yaml_dumper import dump_yaml, spell_inline, spell_value
from dbprint.spec.rounding import UnrepresentableValue


class TestDriverScalars:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (
                uuid.UUID("3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
                "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            ),
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


class TestExactDecimals:
    """SPEC 2.2.6: an exact number is written positionally with every digit, never quoted."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (decimal.Decimal("0.123456789012345678"), "v: 0.123456789012345678"),
            (decimal.Decimal("1E-7"), "v: 0.0000001"),
            (decimal.Decimal("-20.0"), "v: -20.0"),
        ],
        ids=["every-digit", "no-exponent", "keeps-a-decimal-point"],
    )
    def test_written_as_a_float_scalar(self, value: decimal.Decimal, expected: str) -> None:
        assert dump_yaml({"v": value}).strip() == expected

    def test_a_non_finite_decimal_is_written_as_the_float_would_be(self) -> None:
        assert dump_yaml({"v": decimal.Decimal("NaN")}) == "v: .nan\n"


class _Spelled(str):
    pass


class TestUnrepresentableTypes:
    @pytest.mark.parametrize(
        "value",
        [
            object(),
            {"red"},
            frozenset({"red"}),
            b"ab",
            bytearray(b"ab"),
            memoryview(b"ab"),
            _Spelled("a"),
        ],
        ids=["object", "set", "frozenset", "bytes", "bytearray", "memoryview", "str-subclass"],
    )
    def test_a_value_with_no_artifact_spelling_is_refused_by_name(self, value: object) -> None:
        """`SafeDumper` writes a set as `!!set` and bytes as `!!binary`; `check` passed both."""

        with pytest.raises(UnrepresentableValue, match=type(value).__qualname__):
            dump_yaml({"v": value})


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
            (decimal.Decimal("1.500"), "1.5"),
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
            "decimal-is-an-unquoted-number",
        ],
    )
    def test_shapes(self, value: object, expected: str) -> None:
        assert spell_inline(value) == expected

    def test_carries_no_document_end_marker(self) -> None:
        """A bare scalar document normally gets PyYAML's own `...` end-of-document line."""

        assert "\n" not in spell_inline(True)
        assert "..." not in spell_inline(True)


class TestSpellValue:
    """One physical line that `yaml.safe_load` turns back into exactly the stored value."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("NULL", "'NULL'"),
            ("", "''"),
            (" ", "' '"),
            ("a / b", "'a / b'"),
            ("x  y", "'x  y'"),
            ("resolved", "resolved"),
            ("café", "café"),
            ("line1\nline2", '"line1\\nline2"'),
            ("a\r\nb", '"a\\r\\nb"'),
            ("tab\there", '"tab\\there"'),
            ("\u200b", '"\\u200b"'),
            ("\xa0", '"\\xa0"'),
            ("\x1b", '"\\x1b"'),
            (None, "NULL"),
        ],
        ids=[
            "stored-null-word",
            "empty",
            "blank",
            "separator",
            "double-space",
            "bare-token",
            "accented",
            "lf",
            "crlf",
            "tab",
            "zero-width-space",
            "nbsp",
            "escape",
            "genuine-null",
        ],
    )
    def test_spelling(self, value: object, expected: str) -> None:
        spelled = spell_value(value)

        assert spelled == expected
        assert value is None or yaml.safe_load(spelled) == value

    def test_a_long_spaced_value_stays_on_one_line(self) -> None:
        value = " ".join(["seedbank"] * 12)

        assert "\n" not in spell_inline(value)
        assert yaml.safe_load(spell_inline(value)) == value

    def test_a_container_escapes_what_it_holds_on_one_line(self) -> None:
        value = {"in": ["line1\nline2", "\u200b", "x, y"]}

        spelled = spell_inline(value)

        assert spelled == '{in: ["line1\\nline2", "\\u200b", \'x, y\']}'
        assert yaml.safe_load(spelled) == value


_SCALARS = (
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False)
    | st.text(st.characters(codec="utf-8"))
)
_VALUES = st.recursive(
    _SCALARS,
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=12,
)


_INVISIBLE = frozenset({"Cc", "Cf", "Zl", "Zp"})


class TestSpellInlineProperties:
    @given(_VALUES)
    @example("\x85")
    @example(["tab\there", "\u200b"])
    def test_a_spelling_loads_back_to_the_value_on_one_line(self, value: object) -> None:
        text = spell_inline(value)

        assert "\n" not in text
        assert not [char for char in text if unicodedata.category(char) in _INVISIBLE]
        assert yaml.safe_load(text) == value


class TestDumpYamlProperties:
    @given(st.dictionaries(st.text(), _VALUES, max_size=6))
    @example({"\x85": None})
    @example({"values": ["a\x85b"]})
    def test_a_dumped_payload_loads_back_to_itself(self, payload: dict[str, object]) -> None:
        assert yaml.safe_load(dump_yaml(payload)) == payload

    @given(st.sets(st.integers(), max_size=3) | st.binary(max_size=8))
    def test_a_set_or_bytes_value_is_refused(self, value: object) -> None:
        with pytest.raises(UnrepresentableValue):
            dump_yaml({"values": value})
