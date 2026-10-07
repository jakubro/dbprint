"""Table-level facts every consumer surface states the same way, read once and worded per surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from dbprint.spec.absence import block_value, column_value
from dbprint.spec.scope import ScanScope, rows_scanned
from dbprint.spec.value_text import spell_percent


GrainState = Literal["keys", "exhausted", "bounded", "not_determined"]
Saturation = Literal["row_count", "scanned_rows"]


@dataclass(frozen=True)
class GrainReading:
    """What identifies a row (SPEC 2.2.12): the keys found, else how the search ended.

    A search block recording no outcome, like an absent one, is `not_determined`.
    """

    keys: list[dict[str, Any]]
    state: GrainState


def grain_reading(
    statistics: dict[str, Any],
    annotated_grain: dict[str, Any] | None,
) -> GrainReading:
    """The producer's grain keys, then any a human annotated (SPEC 2.7.1), or the search outcome."""

    block = block_value(statistics, "grain")
    keys = [k for k in (block.get("keys") or []) if isinstance(k, dict)] if block else []
    keys = keys + _annotated_grain_keys(annotated_grain)

    if keys:
        return GrainReading(keys, "keys")

    search = block.get("search") if block else None
    exhausted = search.get("exhausted") if isinstance(search, dict) else None

    if exhausted is True:
        return GrainReading([], "exhausted")
    elif exhausted is False:
        return GrainReading([], "bounded")
    else:
        return GrainReading([], "not_determined")


@dataclass(frozen=True)
class LayoutReading:
    """The declared clustering/partitioning key (SPEC 2.2.11) - a schema fact, never a claim."""

    mechanism: Any
    keys: list[dict[str, Any]]


def physical_layout(statistics: dict[str, Any]) -> LayoutReading | None:
    """The `physical_layout` block's mechanism and its well-formed keys, or None without one."""

    block = block_value(statistics, "physical_layout")

    if not isinstance(block, dict):
        return None

    return LayoutReading(
        block.get("mechanism"),
        [k for k in block.get("keys") or [] if isinstance(k, dict)],
    )


def scanned_row_share(coverage: Any) -> str | None:
    """A block's `coverage` as words; `1.0` alone is every scanned row (SPEC 2.2.16), else None."""

    if isinstance(coverage, bool) or not isinstance(coverage, int | float):
        return None

    return "every scanned row" if coverage >= 1 else f"{spell_percent(coverage)} of scanned rows"


def connection_statistics_params(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest's connection-level `statistics_params`; a value that is not a mapping is none."""

    params = manifest.get("statistics_params")

    return params if isinstance(params, dict) else {}


def effective_statistics_params(
    connection_params: Mapping[str, Any],
    table_params: Any,
) -> dict[str, Any]:
    """`connection_params` overridden key by key by one table's own (SPEC 2.5)."""

    return {**connection_params, **(table_params if isinstance(table_params, dict) else {})}


def saturation(
    column: dict[str, Any],
    row_count: int | None,
    scope: ScanScope | None,
) -> Saturation | None:
    """The population a column's distinct count equals, if any - scanned rows under a scope
    (SPEC 2.2.8), else the table's own `row_count` from its statistics; never at zero.
    """

    cardinality = column_value(column, "cardinality")

    if cardinality is None:
        return None
    elif scope is not None:
        scanned = rows_scanned(column, scope)

        return "scanned_rows" if scanned and cardinality == scanned else None
    else:
        return "row_count" if row_count and cardinality == row_count else None


def live_annotations(
    annotated_columns: Mapping[str, Any] | None,
    statistics: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Per-column human notes whose column the statistics still list (SPEC 2.7.1).

    A key naming an absent column is stale; with no columns map to judge by, every key stands.
    """

    entries = {
        name: entry for name, entry in (annotated_columns or {}).items() if isinstance(entry, dict)
    }

    known = (statistics or {}).get("columns")

    if not isinstance(known, dict):
        return entries

    return {name: entry for name, entry in entries.items() if name in known}


def _annotated_grain_keys(annotated_grain: dict[str, Any] | None) -> list[dict[str, Any]]:
    keys = (annotated_grain or {}).get("keys")

    if not isinstance(keys, list):
        return []

    result = []

    for key in keys:
        if not isinstance(key, dict):
            continue

        entry = {"columns": key.get("columns") or [], "detection": "annotated"}
        note = key.get("note")

        if isinstance(note, str) and note.strip():
            entry["note"] = note

        result.append(entry)

    return result
