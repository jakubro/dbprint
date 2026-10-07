"""Split a Markdown grammar line the way a reader must: never inside a quoted literal."""

from __future__ import annotations


def split_outside_literals(text: str, separator: str) -> list[str]:
    """`text` split on `separator` wherever it falls outside a `'...'` or `"..."` literal.

    A doubled `''` stays inside an SQL literal, and a backslash escapes inside a double-quoted one.
    """

    out: list[str] = []
    current = ""
    quote: str | None = None
    i = 0

    while i < len(text):
        char = text[i]

        if quote == "'" and text.startswith("''", i):
            current += "''"
            i += 2
            continue

        if quote == '"' and char == "\\":
            current += text[i : i + 2]
            i += 2
            continue

        if quote is None and char in "'\"":
            quote = char
        elif char == quote:
            quote = None
        elif quote is None and text.startswith(separator, i):
            out.append(current)
            current = ""
            i += len(separator)
            continue

        current += char
        i += 1

    return [*out, current]
