"""How every surface reads a column's value list: each entry's count, each spelling under its
canonical value (SPEC 2.2.4), and the notes a human attached to a value (SPEC 2.7.1).
"""

from __future__ import annotations

from typing import Any


def count_of(entry: Any) -> int:
    """An entry's `count` read as JSON Schema's `integer`: `25.0` is 25, anything else 0."""

    count = entry.get("count") if isinstance(entry, dict) else None

    if isinstance(count, bool):
        return 0
    elif isinstance(count, int):
        return count
    elif isinstance(count, float) and count.is_integer():
        return int(count)
    else:
        return 0


def value_key(value: Any) -> str:
    """YAML reads `1` and `'1'` as different scalars; the string form matches either spelling."""

    return str(value)


def grouped_values(entries: list[Any]) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Canonical entries, each with the lesser spellings of it, most rows first.

    Where a value has several spellings, a group ranks by their joint count, ties in the list's own
    order; a list with none keeps its own order. A member naming a value the list lacks stands
    alone: dropped, its literal would vanish.
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

    groups = [
        (entry, members.get(value_key(entry.get("value")), []))
        for entry in entries
        if isinstance(entry, dict)
        and (entry.get("spelling_of") is None or value_key(entry["spelling_of"]) not in listed)
    ]

    if not any(members for _, members in groups):
        return groups

    return sorted(groups, key=lambda group: -sum(count_of(e) for e in (group[0], *group[1])))


def value_notes(annotation: Any) -> dict[str, str]:
    """A column's per-value notes, keyed by `value_key`; whitespace collapsed, blanks dropped."""

    return {key: note for key, (_, note) in noted_values(annotation).items()}


def noted_values(annotation: Any) -> dict[str, tuple[Any, str]]:
    """`value_notes` with each note's value as the file writes it, in the file's order."""

    if not isinstance(annotation, dict):
        return {}

    entries = annotation.get("values")

    if not isinstance(entries, list):
        return {}

    return {
        value_key(entry.get("value")): (entry.get("value"), " ".join(entry["note"].split()))
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("note"), str) and entry["note"].strip()
    }
