"""How much of a table a `statistics.yaml` describes (SPEC 2.2.8), read the same way by every consumer.

A scoped file describes the rows scanned, not the table. Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .absence import Absence, block_value, column_value, read_table_block
from .value_text import spell_number, spell_percent


SCANNED_CLAUSE = "over the rows scanned"
WHOLE_DOMAIN_STATEMENT = "the list is the whole domain"
SCANNED_DOMAIN_STATEMENT = f"{WHOLE_DOMAIN_STATEMENT} {SCANNED_CLAUSE}"
SAMPLED_STATEMENT = "a sample of the most frequent values"


@dataclass(frozen=True)
class ScanScope:
    """A scoped file's reading: the `scope` block verbatim (`{}` when malformed or echo-only)."""

    block: dict[str, Any]
    rows_scanned: int | None
    row_count: int | None
    sample: float | None
    filter: str | None
    share: float | None


def scope_of(statistics: Mapping[str, Any] | None) -> ScanScope | None:
    """The file's scope, or None when it read every row.

    A present `scope` decides, malformed or not; else any column's `rows_scanned` echo makes it scoped.
    """

    if not statistics:
        return None

    reading = read_table_block(statistics, "scope")
    columns = statistics.get("columns")
    echoes = [
        echo
        for col in (columns.values() if isinstance(columns, Mapping) else ())
        if isinstance(col, Mapping)
        and isinstance(echo := column_value(col, "rows_scanned"), int)
        and not isinstance(echo, bool)
    ]

    if reading.state is not Absence.PRESENT and not echoes:
        return None

    block = dict(reading.value) if isinstance(reading.value, Mapping) else {}
    rows_scanned = block.get("rows_scanned")

    if not _is_int(rows_scanned):
        rows_scanned = echoes[0] if echoes else None

    row_count = block_value(statistics, "row_count")
    row_count = row_count if _is_int(row_count) else None
    sample = block.get("sample")
    filter_ = block.get("filter")
    share = rows_scanned / row_count if rows_scanned is not None and row_count else None

    return ScanScope(
        block=block,
        rows_scanned=rows_scanned,
        row_count=row_count,
        sample=sample
        if isinstance(sample, (int, float)) and not isinstance(sample, bool)
        else None,
        filter=filter_ if isinstance(filter_, str) else None,
        share=share,
    )


def rows_scanned(column: Mapping[str, Any], scope: ScanScope | None) -> int | None:
    """Rows a scoped column was measured over - its own echo, else the file's; None unscoped."""

    if scope is None:
        return None

    echo = column_value(column, "rows_scanned")

    return echo if _is_int(echo) else scope.rows_scanned


def list_is_complete(column: Mapping[str, Any]) -> bool:
    """Whether `values` lists every distinct value over what was read (SPEC 2.2.4, 2.2.5).

    Without a `values_coverage`, `frequencies.listed` against an exact `cardinality` decides.
    """

    if column_value(column, "values_coverage") == 1.0:
        return True

    frequencies = column_value(column, "frequencies")
    cardinality = column_value(column, "cardinality")

    return (
        isinstance(frequencies, Mapping)
        and column_value(column, "cardinality_method") == "exact"
        and _is_int(cardinality)
        and frequencies.get("listed") == cardinality
    )


def list_is_table_domain(column: Mapping[str, Any], scope: ScanScope | None) -> bool:
    """Whether `values` is the column's whole domain over the table, not just the rows read."""

    return scope is None and list_is_complete(column)


def qualify(text: str, scope: ScanScope | None) -> str:
    """Append `SCANNED_CLAUSE` to a claim a single unread row could falsify; unscoped, unchanged."""

    return text if scope is None else f"{text} {SCANNED_CLAUSE}"


def coverage_statement(coverage: float, scope: ScanScope | None) -> str:
    """What a value list at `coverage` covers - the whole domain, over the scan, or a sample."""

    if coverage != 1.0:
        return SAMPLED_STATEMENT

    return SCANNED_DOMAIN_STATEMENT if scope is not None else WHOLE_DOMAIN_STATEMENT


def scope_line(scope: ScanScope) -> str:
    """The `Scanned:` line: rows read, their share of `row_count`, and how the read was narrowed.

    Its share is of `row_count`: `sample` is what was asked for, not what came, so is not printed.
    """

    if scope.rows_scanned is None:
        scanned = "part of the table"
    elif scope.share is not None and scope.row_count is not None:
        rows, total = spell_number(scope.rows_scanned), spell_number(scope.row_count)
        scanned = f"{rows} of {total} rows ({spell_percent(scope.share)})"
    else:
        scanned = f"{spell_number(scope.rows_scanned)} rows"

    return f"Scanned: {scanned}{_narrowing_suffix(scope)}"


def reply_scope(scope: ScanScope | None) -> dict[str, Any]:
    """The `scope` and `row_count` fields a structured reply about a scoped table carries."""

    if scope is None:
        return {}

    reply: dict[str, Any] = {"scope": scope.block}

    if scope.row_count is not None:
        reply["row_count"] = scope.row_count

    return reply


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _narrowing_suffix(scope: ScanScope) -> str:
    if scope.sample is not None:
        return "; sampled"

    if scope.filter is not None and scope.filter.strip():
        return f"; filtered by `{scope.filter}`"

    return ""
