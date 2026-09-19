"""Resolve a phrase against one column's published values (MCP.md 4.7).

Reads a column's list, counts and notes, never a database; spellings fold per SPEC 2.2.4.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from dbprint.spec.normalization import fold


NEAREST_FLOOR = 0.3  # Jaccard over character trigrams; below this a "nearest" is noise
NEAREST_LIMIT = 5
DOMAIN_LIMIT = 50  # an exhaustive list this short rides along, so one call answers everything

SAMPLE_CAVEAT = (
    "the print lists {listed} of the column's values; a spelling absent here is not evidence "
    "that it is absent from the column"
)

_TRIGRAM_PAD = 2


def spelling_groups(entries: Sequence[tuple[Any, int]]) -> dict[int, Any]:
    """Map each lesser spelling's position to the value it is a spelling of (SPEC 2.2.4).

    Two entries group when their string values fold to one key; the canonical member is the
    most frequent, ties going to the order the list already fixes.
    """

    by_key: dict[str, list[int]] = {}

    for index, (value, _count) in enumerate(entries):
        if isinstance(value, str):
            by_key.setdefault(fold(value), []).append(index)

    grouped: dict[int, Any] = {}

    for members in by_key.values():
        if len(members) < 2:
            continue

        canonical = max(members, key=lambda index: (entries[index][1], -index))
        grouped.update({index: entries[canonical][0] for index in members if index != canonical})

    return grouped


def resolve(
    text: str,
    entries: list[Any],
    notes: dict[str, str],
    *,
    coverage: float | None,
    exhaustive: bool | None = None,
    unavailable_reason: str | None = None,
) -> dict[str, Any]:
    """Answer `text` against one column's listed values, in the order MCP.md 4.7 declares.

    `exhaustive` defaults to `coverage == 1.0`; a numeric list has none and the caller decides.
    """

    listed = [entry for entry in entries if isinstance(entry, dict)]

    if exhaustive is None:
        exhaustive = coverage == 1.0

    if unavailable_reason is not None:
        return {
            "match": "unavailable",
            "reason": unavailable_reason,
            "coverage": coverage,
            "exhaustive": exhaustive,
            "listed": len(listed),
        }

    reply: dict[str, Any] = {"coverage": coverage, "exhaustive": exhaustive, "listed": len(listed)}

    if not exhaustive:
        reply["sample_caveat"] = SAMPLE_CAVEAT.format(listed=len(listed))

    if exhaustive and len(listed) <= DOMAIN_LIMIT:
        reply["domain"] = [_entry_payload(entry, notes) for entry in listed]

    stored = _stored(text, listed, notes)

    if stored is not None:
        return {**reply, "match": "stored", "spellings": stored}

    defined = _by_definition(text, listed, notes)

    if defined:
        return {**reply, "match": "definition", "candidates": defined}

    nearest = _nearest(text, listed, notes)

    if nearest:
        return {**reply, "match": "nearest", "candidates": nearest}

    return {**reply, "match": "none"}


def _stored(text: str, listed: list[dict[str, Any]], notes: dict[str, str]) -> list[Any] | None:
    """Every listed spelling `text` is or folds to - all of them, because they are one category.

    Answering with one spelling where the column holds several is what makes a predicate miss
    the rows stored under the others.
    """

    key = fold(text)
    spellings = [
        _entry_payload(entry, notes)
        for entry in listed
        if entry.get("value") is not None
        and (str(entry["value"]) == text or fold(str(entry["value"])) == key)
    ]

    return spellings or None


def _by_definition(
    text: str,
    listed: list[dict[str, Any]],
    notes: dict[str, str],
) -> list[dict[str, Any]]:
    """Values whose note contains `text` as a whole phrase, or is contained in it."""

    phrase = fold(text)

    if not phrase:
        return []

    pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")
    matched = []

    for entry in listed:
        note = notes.get(_key_of(entry.get("value")))

        if not note:
            continue

        folded_note = fold(note)

        note_pattern = re.compile(rf"(?<!\w){re.escape(folded_note)}(?!\w)")

        if pattern.search(folded_note) or note_pattern.search(phrase):
            matched.append(_entry_payload(entry, notes))

    return matched


def _nearest(
    text: str,
    listed: list[dict[str, Any]],
    notes: dict[str, str],
) -> list[dict[str, Any]]:
    """The listed values closest to `text` by trigram overlap, best first."""

    wanted = _trigrams(fold(text))

    if not wanted:
        return []

    scored = []

    for entry in listed:
        value = entry.get("value")

        if value is None:
            continue

        score = _similarity(wanted, _trigrams(fold(str(value))))

        if score >= NEAREST_FLOOR:
            scored.append((score, entry))

    scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("value"))))

    return [
        {**_entry_payload(entry, notes), "score": round(score, 4)}
        for score, entry in scored[:NEAREST_LIMIT]
    ]


def _entry_payload(entry: dict[str, Any], notes: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {"value": entry.get("value"), "count": entry.get("count")}
    note = notes.get(_key_of(entry.get("value")))

    if note:
        payload["note"] = note

    spelling_of = entry.get("spelling_of")

    if spelling_of is not None:
        payload["spelling_of"] = spelling_of

    return payload


def _key_of(value: Any) -> str:
    """YAML reads `1` and `'1'` as different scalars; the string form matches either."""

    return str(value)


def _trigrams(text: str) -> set[str]:
    """Character trigrams over a padded string, so a short value still has overlap to score."""

    padded = " " * _TRIGRAM_PAD + text + " " * _TRIGRAM_PAD

    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def _similarity(left: set[str], right: set[str]) -> float:
    """Jaccard overlap; 0 where either side is empty."""

    if not left or not right:
        return 0.0

    return len(left & right) / len(left | right)
