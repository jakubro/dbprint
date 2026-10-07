"""Layout-key lists split on top-level commas only (SPEC 2.2.11)."""

from __future__ import annotations

import pytest

from dbprint.adapters.sql_layout import split_top_level


@pytest.mark.parametrize(
    ("text", "quote_char", "parts"),
    [
        ("a, b", '"', ["a", " b"]),
        ("intDiv(id, 1000), d", "`", ["intDiv(id, 1000)", " d"]),
        ('"a,b", c', '"', ['"a,b"', " c"]),
        ("`x(y`, z", "`", ["`x(y`", " z"]),
        ("substr(s, ','), t", '"', ["substr(s, ',')", " t"]),
    ],
)
def test_a_comma_inside_a_call_or_a_quote_does_not_split(
    text: str,
    quote_char: str,
    parts: list[str],
) -> None:
    assert split_top_level(text, quote_char) == parts
