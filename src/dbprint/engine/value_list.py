"""How every surface reads a column's value list: each spelling under its canonical value
(SPEC 2.2.4), and the notes a human attached to a value (SPEC 2.7.1).
"""

from __future__ import annotations

from typing import Any


def value_key(value: Any) -> str:
    """YAML reads `1` and `'1'` as different scalars; the string form matches either spelling."""

    return str(value)


def grouped_values(entries: list[Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Canonical entries in the list's own order, each with the lesser spellings of it.

    A member naming a value the list lacks stands alone: dropped, its literal would vanish.
    """

    listed = {
        value_key(entry.get("value"))
        for entry in entries
        if isinstance(entry, dict) and entry.get("spelling_of") is None
    }
    members: dict[str, list[dict[str, Any]]] = {}

    for entry in entries:
        if isinstance(entry, dict) and entry.get("spelling_of") is not None:
            members.setdefault(value_key(entry["spelling_of"]), []).append(entry)

    return [
        (entry, members.get(value_key(entry.get("value")), []))
        for entry in entries
        if isinstance(entry, dict)
        and (entry.get("spelling_of") is None or value_key(entry["spelling_of"]) not in listed)
    ]


def value_notes(annotation: Any) -> dict[str, str]:
    """A column's per-value notes, keyed by `value_key`; whitespace collapsed, blanks dropped."""

    if not isinstance(annotation, dict):
        return {}

    entries = annotation.get("values")

    if not isinstance(entries, list):
        return {}

    return {
        value_key(entry.get("value")): " ".join(entry["note"].split())
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("note"), str) and entry["note"].strip()
    }
