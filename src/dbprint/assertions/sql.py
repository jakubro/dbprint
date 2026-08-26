"""SQL assertion evaluator, online mode only - queries run via `adapter.execute_query`.

Each result is compared against the declared `expect` (0 or empty) per ASSERTIONS.md 3.
"""

from __future__ import annotations

from typing import Any, Protocol

from dbprint.conformance.issue import Issue
from . import issue as codes
from .parser import AssertionSet, QueryAssertion


SPEC_REF = "ASSERTIONS.md §3"
ROW_TRUNCATE_LIMIT = 10  # max rows listed in assertion.sql-non-empty detail


class _CursorExecutor(Protocol):
    """Minimal adapter interface a SQL assertion needs; structural, so tests can pass a stub."""

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]: ...


def evaluate(
    assertion_set: AssertionSet,
    connection_name: str,
    executor: _CursorExecutor,
) -> list[Issue]:
    """Run every SQL assertion query; return issues in deterministic order."""

    issues: list[Issue] = []

    for query in assertion_set.queries:
        issues.extend(_evaluate_query(connection_name, query, executor))

    issues.sort()

    return issues


def _evaluate_query(
    connection_name: str,
    query: QueryAssertion,
    executor: _CursorExecutor,
) -> list[Issue]:
    path = f"assertions.{connection_name}.queries.{query.name}"

    try:
        rows = executor.execute_query(query.sql)
    except Exception as exc:  # noqa: BLE001 - run-all-then-report; the error becomes an Issue
        return [
            Issue(
                path=path,
                code=codes.SQL_EXECUTION_ERROR,
                severity=query.severity,
                detail=f"DB error: {exc}",
                spec_ref=SPEC_REF,
            ),
        ]

    if query.expect == "0":
        return _evaluate_expect_zero(path, query, rows)

    return _evaluate_expect_empty(path, query, rows)


def _evaluate_expect_zero(
    path: str,
    query: QueryAssertion,
    rows: list[tuple[Any, ...]],
) -> list[Issue]:
    if not rows:
        return [
            Issue(
                path=path,
                code=codes.SQL_EMPTY_RESULT,
                severity=query.severity,
                detail="query returned zero rows; expect: 0 requires a scalar result",
                spec_ref=SPEC_REF,
            ),
        ]

    first_row = rows[0]

    if not first_row:
        return [
            Issue(
                path=path,
                code=codes.SQL_EMPTY_RESULT,
                severity=query.severity,
                detail="query returned a row with no columns",
                spec_ref=SPEC_REF,
            ),
        ]

    actual = first_row[0]

    if actual is None:
        return [
            Issue(
                path=path,
                code=codes.SQL_NON_ZERO,
                severity=query.severity,
                detail="actual: null (expected: 0)",
                spec_ref=SPEC_REF,
            ),
        ]
    elif isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return [
            Issue(
                path=path,
                code=codes.SQL_TYPE_COERCION_ERROR,
                severity=query.severity,
                detail=f"actual value {actual!r} not coercible to integer",
                spec_ref=SPEC_REF,
            ),
        ]
    elif actual == 0:
        return []
    else:
        return [
            Issue(
                path=path,
                code=codes.SQL_NON_ZERO,
                severity=query.severity,
                detail=f"actual: {actual} (expected: 0)",
                spec_ref=SPEC_REF,
            ),
        ]


def _evaluate_expect_empty(
    path: str,
    query: QueryAssertion,
    rows: list[tuple[Any, ...]],
) -> list[Issue]:
    if not rows:
        return []

    sample = rows[:ROW_TRUNCATE_LIMIT]
    detail = f"returned {len(rows)} row(s); first {len(sample)}: {sample!r}"

    if len(rows) > ROW_TRUNCATE_LIMIT:
        detail += f" (... {len(rows) - ROW_TRUNCATE_LIMIT} more)"

    return [
        Issue(
            path=path,
            code=codes.SQL_NON_EMPTY,
            severity=query.severity,
            detail=detail,
            spec_ref=SPEC_REF,
        ),
    ]
