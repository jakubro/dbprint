"""Assemble SQL lists, wrappers and calls built in code (GUIDELINES.md 2, `adapters`).

A fragment is built at column zero; the statement it lands in says how deep it sits.
"""

from __future__ import annotations

from collections.abc import Iterable


_CALL_WIDTH = 80


def listed(items: Iterable[str], indent: int) -> str:
    """Join list items one per line with trailing commas, every line after the first `indent` deep."""

    return ",\n".join(items).replace("\n", "\n" + " " * indent)


def indented(fragment: str, indent: int) -> str:
    """Shift every line of `fragment` after the first `indent` spaces deeper."""

    return fragment.replace("\n", "\n" + " " * indent)


def select_from(items: Iterable[str], source: str) -> str:
    """A statement reading one source whole: the select list one item per line, then `FROM`."""

    return f"SELECT\n  {listed(items, 2)}\nFROM\n  {indented(source, 2)}"


def derived(statement: str, alias: str) -> str:
    """`statement` as a derived table: `(` ending the line, the body two deep, `) alias` closing."""

    return f"(\n  {indented(statement, 2)}\n) {alias}"


def call(name: str, *args: str) -> str:
    """`name(args)` on one line when it fits in 80 characters, else one argument per line."""

    flat = f"{name}({', '.join(args)})"

    if "\n" not in flat and len(flat) <= _CALL_WIDTH:
        return flat

    return f"{name}(\n  {listed(args, 2)}\n)"


def split_top_level(text: str, quote_char: str) -> list[str]:
    """Split `text` on the commas outside parentheses, quoted identifiers and string literals."""

    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quoted: str | None = None

    for ch in text:
        if quoted is not None:
            quoted = None if ch == quoted else quoted
        elif ch in (quote_char, "'"):
            quoted = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue

        current.append(ch)

    parts.append("".join(current))

    return parts


def trimmed_lines(text: str) -> str:
    """`text` with each line's trailing whitespace and the outer blank lines stripped, ending in
    one newline, or empty when nothing is left (SPEC 2.1.3).
    """

    joined = "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")

    return joined + "\n" if joined else ""
