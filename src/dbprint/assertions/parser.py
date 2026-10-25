"""Parse the `assertions:` block from .dbprint.yaml into typed records.

The block declares `tables.<fqn>` predicates (row_count, columns.<column>.<stat>) and a
`queries` list of name/severity/sql/expect entries (ASSERTIONS.md 1.2). A malformed shape
becomes a ParseFault rather than aborting; only a non-mapping block raises.
"""

from __future__ import annotations

import difflib
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from dbprint.spec.predicate import (
    ASSERTABLE_STATS,
    RANGE_BOUNDS,
    is_assertable_stat,
    is_value_bearing_stat,
    takes_range,
)
from . import issue as codes


Severity = Literal["error", "warning"]
ExpectKind = Literal["0", "empty"]

# Each ParseFault cites the section it violated: 1.2 for shape, 3.5 for duplicate names.
_BLOCK_SPEC_REF = "ASSERTIONS.md §1.2"
_DUPLICATE_SPEC_REF = "ASSERTIONS.md §3.5"
_PREDICATE_SPEC_REF = "ASSERTIONS.md §2.1"
_VOCABULARY_SPEC_REF = "ASSERTIONS.md §2.4"

_BLOCK_KEYS = ("queries", "tables")
_TABLE_KEYS = ("columns", "row_count")
_QUERY_KEYS = ("expect", "name", "severity", "sql")


class ParseError(ValueError):
    """Raised only when `assertions:` itself cannot be iterated as a mapping.

    Every other malformed shape becomes a ParseFault and leaves sibling entries parsed.
    """


@dataclass(frozen=True)
class ParseFault:
    """One non-aborting defect found while parsing the assertions block.

    `path` is connection-relative; the caller prefixes it with `assertions.<connection>.`.
    `code`/`spec_ref` are set here, since only this module knows which section was violated.
    """

    path: str
    code: str
    detail: str
    spec_ref: str


@dataclass(frozen=True)
class TablePredicates:
    """Per-table predicates - row_count + per-column predicate maps."""

    fqn: str
    row_count: Any | None = None  # raw predicate YAML; passed to predicate.parse
    columns: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class QueryAssertion:
    """One SQL assertion."""

    name: str
    sql: str
    expect: ExpectKind
    severity: Severity = "error"


@dataclass(frozen=True)
class AssertionSet:
    """Typed view of the assertions block for one connection.

    `queries` is ordered and first-occurrence-wins on a name collision; `faults` carries
    every non-aborting defect alongside the predicates the rest of the block produced.
    """

    tables: dict[str, TablePredicates] = field(default_factory=dict)
    queries: tuple[QueryAssertion, ...] = ()
    faults: tuple[ParseFault, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.tables and not self.queries


def parse(raw: Any) -> AssertionSet:
    """Parse a raw assertions dict (post-YAML load) into an AssertionSet.

    Tolerant of missing keys, None bodies and empty maps (ASSERTIONS.md 1.5); raises
    ParseError only when `assertions:` itself is not a mapping.
    """

    if raw is None:
        return AssertionSet()

    if not isinstance(raw, dict):
        raise ParseError(f"assertions: must be a mapping, got {type(raw).__name__}")

    tables_raw = raw.get("tables") or {}
    queries_raw = raw.get("queries") or []

    block_faults = _unknown_keys("", raw, _BLOCK_KEYS)
    tables, table_faults = _parse_tables(tables_raw)
    queries, query_faults = _parse_queries(queries_raw)
    queries, dup_faults = _drop_duplicate_query_names(queries)

    return AssertionSet(
        tables=tables,
        queries=queries,
        faults=block_faults + table_faults + query_faults + dup_faults,
    )


def _parse_tables(raw: Any) -> tuple[dict[str, TablePredicates], tuple[ParseFault, ...]]:
    if not isinstance(raw, dict):
        fault = ParseFault(
            path="tables",
            code=codes.MALFORMED_BLOCK,
            detail=f"assertions.tables: must be a mapping, got {type(raw).__name__}",
            spec_ref=_BLOCK_SPEC_REF,
        )

        return {}, (fault,)

    out: dict[str, TablePredicates] = {}
    faults: list[ParseFault] = []

    for fqn, body in raw.items():
        if body is None:
            body = {}

        if not isinstance(body, dict):
            faults.append(
                ParseFault(
                    path=f"tables.{fqn}",
                    code=codes.MALFORMED_BLOCK,
                    detail=(
                        f"assertions.tables.{fqn}: must be a mapping, got {type(body).__name__}"
                    ),
                    spec_ref=_BLOCK_SPEC_REF,
                ),
            )

            continue

        faults.extend(_unknown_keys(f"tables.{fqn}", body, _TABLE_KEYS))
        row_count, row_count_faults = _parse_row_count(fqn, body)
        faults.extend(row_count_faults)
        columns_raw = body.get("columns") or {}
        columns, column_faults = _parse_columns(fqn, columns_raw)
        faults.extend(column_faults)

        out[fqn] = TablePredicates(fqn=fqn, row_count=row_count, columns=columns)

    return out, tuple(faults)


def _parse_row_count(fqn: str, body: dict[Any, Any]) -> tuple[Any, tuple[ParseFault, ...]]:
    # Present but null is a fault here: the evaluator reads None as "not asserted".
    if "row_count" not in body:
        return None, ()

    path = f"tables.{fqn}.row_count"
    raw = body["row_count"]

    if raw is None:
        fault = ParseFault(
            path=path,
            code=codes.MALFORMED_PREDICATE,
            detail=f"assertions.{path}: a predicate needs a value",
            spec_ref=_PREDICATE_SPEC_REF,
        )

        return None, (fault,)

    faults = _range_key_faults(path, raw)

    return (None if faults else raw), faults


def _parse_columns(
    fqn: str,
    raw: Any,
) -> tuple[dict[str, dict[str, Any]], tuple[ParseFault, ...]]:
    if not isinstance(raw, dict):
        fault = ParseFault(
            path=f"tables.{fqn}.columns",
            code=codes.MALFORMED_BLOCK,
            detail=(
                f"assertions.tables.{fqn}.columns: must be a mapping, got {type(raw).__name__}"
            ),
            spec_ref=_BLOCK_SPEC_REF,
        )

        return {}, (fault,)

    columns: dict[str, dict[str, Any]] = {}
    faults: list[ParseFault] = []

    for col_name, col_body in raw.items():
        if col_body is None:
            col_body = {}

        if not isinstance(col_body, dict):
            faults.append(
                ParseFault(
                    path=f"tables.{fqn}.columns.{col_name}",
                    code=codes.MALFORMED_BLOCK,
                    detail=(
                        f"assertions.tables.{fqn}.columns.{col_name}: must be a mapping, "
                        f"got {type(col_body).__name__}"
                    ),
                    spec_ref=_BLOCK_SPEC_REF,
                ),
            )

            continue

        stats: dict[str, Any] = {}

        for stat, predicate in col_body.items():
            path = f"tables.{fqn}.columns.{col_name}.{stat}"

            if not _names_a_stat(str(stat)):
                faults.append(_unknown_stat(path, str(stat)))
            elif (
                is_assertable_stat(str(stat))
                and takes_range(str(stat))
                and (range_faults := _range_key_faults(path, predicate))
            ):
                faults.extend(range_faults)
            else:
                stats[stat] = predicate

        columns[col_name] = stats

    return columns, tuple(faults)


def _names_a_stat(stat: str) -> bool:
    # `range` and `percentiles` alone stay with the evaluator, which refuses them on a redacted
    # column as a warning rather than an error (ASSERTIONS.md 2.4).
    return is_assertable_stat(stat) or (is_value_bearing_stat(stat) and "." not in stat)


def _unknown_stat(path: str, stat: str) -> ParseFault:
    close = difflib.get_close_matches(stat, sorted(ASSERTABLE_STATS), n=1, cutoff=0.6)
    hint = f"; did you mean '{close[0]}'?" if close else ""

    return ParseFault(
        path=path,
        code=codes.UNKNOWN_STAT,
        detail=f"assertions.{path}: stat {stat!r} not in §2.4 vocabulary{hint}",
        spec_ref=_VOCABULARY_SPEC_REF,
    )


def _range_key_faults(path: str, raw: Any) -> tuple[ParseFault, ...]:
    if not isinstance(raw, dict):
        return ()

    return _unknown_keys(
        path,
        raw,
        RANGE_BOUNDS,
        code=codes.MALFORMED_PREDICATE,
        spec_ref=_PREDICATE_SPEC_REF,
    )


def _parse_queries(raw: Any) -> tuple[tuple[QueryAssertion, ...], tuple[ParseFault, ...]]:
    if not isinstance(raw, list):
        fault = ParseFault(
            path="queries",
            code=codes.MALFORMED_BLOCK,
            detail=f"assertions.queries: must be a list, got {type(raw).__name__}",
            spec_ref=_BLOCK_SPEC_REF,
        )

        return (), (fault,)

    out: list[QueryAssertion] = []
    faults: list[ParseFault] = []

    for idx, entry in enumerate(raw):
        parsed, entry_faults = _parse_one_query(idx, entry)
        faults.extend(entry_faults)

        if parsed is not None:
            out.append(parsed)

    return tuple(out), tuple(faults)


def _parse_one_query(
    idx: int,
    entry: Any,
) -> tuple[QueryAssertion | None, tuple[ParseFault, ...]]:
    if not isinstance(entry, dict):
        return None, (
            ParseFault(
                path=f"queries[{idx}]",
                code=codes.MALFORMED_BLOCK,
                detail=f"assertions.queries[{idx}]: must be a mapping, got {type(entry).__name__}",
                spec_ref=_BLOCK_SPEC_REF,
            ),
        )

    entry_dict = cast("dict[str, Any]", entry)
    name = entry_dict.get("name")
    sql = entry_dict.get("sql")
    expect = entry_dict.get("expect")
    severity = entry_dict.get("severity", "error")
    named = isinstance(name, str) and bool(name)
    unknown = _unknown_keys(f"queries.{name}" if named else f"queries[{idx}]", entry, _QUERY_KEYS)

    if not isinstance(name, str) or not name:
        return None, (
            *unknown,
            ParseFault(
                path=f"queries[{idx}]",
                code=codes.MALFORMED_BLOCK,
                detail=f"assertions.queries[{idx}]: `name` is required and must be a string",
                spec_ref=_BLOCK_SPEC_REF,
            ),
        )

    if not isinstance(sql, str) or not sql.strip():
        return None, (
            *unknown,
            ParseFault(
                path=f"queries.{name}",
                code=codes.MALFORMED_BLOCK,
                detail=f"assertions.queries[{idx}] {name!r}: `sql` is required",
                spec_ref=_BLOCK_SPEC_REF,
            ),
        )

    if isinstance(expect, bool) or expect not in {0, "0", "empty"}:
        return None, (
            *unknown,
            ParseFault(
                path=f"queries.{name}",
                code=codes.MALFORMED_BLOCK,
                detail=f"assertions.queries[{idx}] {name!r}: `expect` must be 0 or empty",
                spec_ref=_BLOCK_SPEC_REF,
            ),
        )

    expect_norm: ExpectKind = "0" if expect in {0, "0"} else "empty"

    if severity not in {"error", "warning"}:
        severity = "warning"  # per ASSERTIONS.md 7 - unknown severity -> warning

    return QueryAssertion(name=name, sql=sql, expect=expect_norm, severity=severity), unknown


def _drop_duplicate_query_names(
    queries: tuple[QueryAssertion, ...],
) -> tuple[tuple[QueryAssertion, ...], tuple[ParseFault, ...]]:
    """Keep the first query per name; fault every later duplicate.

    A collision is a per-entry defect, so sibling queries and table predicates still run
    (ASSERTIONS.md 5.4).
    """

    seen: set[str] = set()
    kept: list[QueryAssertion] = []
    faults: list[ParseFault] = []

    for q in queries:
        if q.name in seen:
            faults.append(
                ParseFault(
                    path=f"queries.{q.name}",
                    code=codes.DUPLICATE_QUERY_NAME,
                    detail=f"assertions.queries: duplicate name {q.name!r} - names must be unique",
                    spec_ref=_DUPLICATE_SPEC_REF,
                ),
            )

            continue

        seen.add(q.name)
        kept.append(q)

    return tuple(kept), tuple(faults)


def _unknown_keys(
    path: str,
    body: dict[Any, Any],
    valid: Collection[str],
    *,
    code: str = codes.MALFORMED_BLOCK,
    spec_ref: str = _BLOCK_SPEC_REF,
) -> tuple[ParseFault, ...]:
    faults: list[ParseFault] = []

    for key in body:
        if (name := str(key)) in valid:
            continue

        where = f"{path}.{name}" if path else name
        close = difflib.get_close_matches(name, sorted(valid), n=1, cutoff=0.6)
        hint = (
            f"did you mean '{close[0]}'?"
            if close
            else f"valid keys here: {', '.join(sorted(valid))}"
        )
        faults.append(
            ParseFault(
                path=where,
                code=code,
                detail=f"assertions.{where}: unknown key; {hint}",
                spec_ref=spec_ref,
            ),
        )

    return tuple(faults)
