"""Statistic assertion evaluator - stat predicates against a per-connection stats map.

`stats_by_fqn` maps FQN -> `{row_count, columns: {col: stats}}`, the shape both offline
statistics.yaml and live re-extraction produce. Output: ordered `assertion.*` Issues.
"""

from __future__ import annotations

from typing import Any

from dbprint.conformance.issue import Issue
from . import issue as codes
from .parser import AssertionSet, TablePredicates
from .predicate import (
    MalformedPredicate,
    Outcome,
    is_assertable_stat,
    is_value_bearing_stat,
)
from .predicate import evaluate as eval_predicate
from .predicate import (
    parse as parse_predicate,
)
from .predicate import (
    resolve as resolve_stat,
)


SPEC_REF = "ASSERTIONS.md §2"


def evaluate(
    assertion_set: AssertionSet,
    connection_name: str,
    stats_by_fqn: dict[str, dict[str, Any]],
) -> list[Issue]:
    """Run every statistic predicate; issues come back sorted, a missing table as a warning."""

    issues: list[Issue] = []

    for fqn, predicates in assertion_set.tables.items():
        stats = stats_by_fqn.get(fqn)

        if stats is None:
            issues.append(
                Issue(
                    path=_table_path(connection_name, fqn),
                    code=codes.UNKNOWN_TABLE,
                    severity="warning",
                    detail=f"table {fqn!r} not in manifest; skipping predicates",
                    spec_ref="ASSERTIONS.md §1.4",
                ),
            )
            continue

        issues.extend(_evaluate_table(connection_name, predicates, stats))

    issues.sort()

    return issues


def _evaluate_table(
    connection_name: str,
    predicates: TablePredicates,
    table_stats: dict[str, Any],
) -> list[Issue]:
    issues: list[Issue] = []

    if predicates.row_count is not None:
        outcome = _check_predicate("row_count", predicates.row_count, table_stats.get("row_count"))

        if not outcome.passed:
            issues.append(
                Issue(
                    path=_row_count_path(connection_name, predicates.fqn),
                    code=_code_for("row_count", outcome),
                    severity="error",
                    detail=outcome.detail,
                    spec_ref=SPEC_REF,
                ),
            )

    columns_stats = table_stats.get("columns") or {}

    for col_name, col_preds in predicates.columns.items():
        col_stats = columns_stats.get(col_name)

        if col_stats is None:
            issues.append(
                Issue(
                    path=_column_path(connection_name, predicates.fqn, col_name, ""),
                    code=codes.UNKNOWN_COLUMN,
                    severity="warning",
                    detail=f"column {col_name!r} not in {predicates.fqn!r} statistics",
                    spec_ref="ASSERTIONS.md §1.4",
                ),
            )
            continue

        for stat, raw in col_preds.items():
            issues.extend(
                _check_column_predicate(
                    connection_name,
                    predicates.fqn,
                    col_name,
                    stat,
                    raw,
                    col_stats,
                ),
            )

    return issues


def _check_column_predicate(
    connection_name: str,
    fqn: str,
    column: str,
    stat: str,
    raw: Any,
    col_stats: dict[str, Any],
) -> list[Issue]:
    """Evaluate one column predicate; emit at most one Issue."""

    # A redacted column's artifact holds placeholders, not real values (SPEC 2.2.9).
    if is_value_bearing_stat(stat) and col_stats.get("redacted") is not None:
        return [
            Issue(
                path=_column_path(connection_name, fqn, column, stat),
                code=codes.REDACTED_STAT,
                severity="warning",
                detail=(
                    f"{stat!r} cannot be evaluated: this column is redacted "
                    f"({col_stats['redacted']}), so its emitted values are not its real ones"
                ),
                spec_ref="§2.2.9",
            ),
        ]

    if not is_assertable_stat(stat):
        return [
            Issue(
                path=_column_path(connection_name, fqn, column, stat),
                code=codes.UNKNOWN_STAT,
                severity="error",
                detail=f"stat {stat!r} not in §2.4 vocabulary",
                spec_ref="ASSERTIONS.md §2.4",
            ),
        ]

    predicate = parse_predicate(stat, raw)

    if isinstance(predicate, MalformedPredicate):
        return [
            Issue(
                path=_column_path(connection_name, fqn, column, stat),
                code=codes.MALFORMED_PREDICATE,
                severity="error",
                detail=predicate.reason,
                spec_ref="ASSERTIONS.md §2.1",
            ),
        ]

    ref = resolve_stat(col_stats, stat)

    if not ref.found:
        return [
            Issue(
                path=_column_path(connection_name, fqn, column, stat),
                code=codes.INAPPLICABLE_STAT,
                severity="warning",
                detail=f"stat {stat!r} not emitted for column {column!r}",
                spec_ref="ASSERTIONS.md §2.6",
            ),
        ]

    outcome = eval_predicate(predicate, ref.value)

    if outcome.passed:
        return []

    return [
        Issue(
            path=_column_path(connection_name, fqn, column, stat),
            code=_code_for(stat, outcome),
            severity="error",
            detail=outcome.detail,
            spec_ref=SPEC_REF,
        ),
    ]


def _check_predicate(stat: str, raw: Any, actual: Any) -> Outcome:
    predicate = parse_predicate(stat, raw)

    if isinstance(predicate, MalformedPredicate):
        return Outcome(passed=False, detail=predicate.reason, malformed=True)

    return eval_predicate(predicate, actual)


def _code_for(stat: str, outcome: Outcome) -> str:
    if outcome.malformed:
        return codes.MALFORMED_PREDICATE
    elif stat.startswith("percentiles."):
        return codes.PERCENTILE_MISMATCH
    else:
        return codes.STAT_TO_FAILURE_CODE[stat]


def _table_path(connection_name: str, fqn: str) -> str:
    return f"assertions.{connection_name}.tables.{fqn}"


def _row_count_path(connection_name: str, fqn: str) -> str:
    return f"assertions.{connection_name}.tables.{fqn}.row_count"


def _column_path(connection_name: str, fqn: str, column: str, stat: str) -> str:
    base = f"assertions.{connection_name}.tables.{fqn}.columns.{column}"

    return f"{base}.{stat}" if stat else base
