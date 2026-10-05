"""statistics.annotations.yaml invariants per SPEC 2.7.1."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from dbprint.spec.absence import column_value, read_column_field
from dbprint.spec.predicate import (
    MalformedPredicate,
    inapplicable_reason,
    is_assertable_stat,
    is_value_bearing_stat,
    with_evidence,
)
from dbprint.spec.predicate import evaluate as eval_predicate
from dbprint.spec.predicate import parse as parse_predicate
from dbprint.spec.predicate import resolve as resolve_stat
from dbprint.spec.redaction import is_redacted
from dbprint.spec.scope import ScanScope, list_is_table_domain, scope_of
from .issue import Issue
from .layout import annotated_tables
from .progress import TableSink


def check_entry(data: Any, path: str, tbl_fqn: str) -> list[Issue]:
    """Check one statistics.annotations.yaml body beyond what the JSON Schema covers.

    Always empty; the schema constrains the shape, and `_check_artifact` dispatches uniformly.
    """

    del data, path, tbl_fqn

    return []


def check_stale_keys(
    print_root: Path,
    manifest_data: dict,
    *,
    on_table: TableSink | None = None,
) -> list[Issue]:
    """Warn on an annotation key naming a column the table's statistics do not have.

    Own pass, since it needs both sibling artifacts. A view's `statistics.yaml` (SPEC 2.2.15)
    names every column its catalog read found, so this reaches a view too.
    """

    issues: list[Issue] = []
    tables = annotated_tables(
        print_root,
        manifest_data,
        "statistics_annotations",
        "statistics",
        on_table,
    )

    for ann_path, ann_data, stats_data in tables:
        columns = ann_data.get("columns")

        if not isinstance(columns, dict):
            continue

        known = set(stats_data.get("columns") or {})
        rel = str(ann_path.relative_to(print_root))

        for name in columns:
            if name not in known:
                issues.append(
                    Issue(
                        f"{rel}::columns.{name}",
                        "annotations.unknown-column",
                        "warning",
                        f"statistics.annotations.yaml names column {name!r}, "
                        "which statistics.yaml does not have.",
                        "§2.7.1",
                    ),
                )

    return issues


def check_grain_annotations(
    print_root: Path,
    manifest_data: dict,
    *,
    on_table: TableSink | None = None,
) -> list[Issue]:
    """Warn on a human-authored grain key naming a column the table's statistics do not have.

    A grain key addresses a SET of columns, not one - any single unknown column in the tuple
    invalidates the whole key, unlike `columns.<name>` which addresses exactly one.
    """

    issues: list[Issue] = []
    tables = annotated_tables(
        print_root,
        manifest_data,
        "statistics_annotations",
        "statistics",
        on_table,
    )

    for ann_path, ann_data, stats_data in tables:
        grain = ann_data.get("grain")

        if not isinstance(grain, dict):
            continue

        keys = grain.get("keys")

        if not isinstance(keys, list):
            continue

        known = set(stats_data.get("columns") or {})
        rel = str(ann_path.relative_to(print_root))

        for i, key in enumerate(keys):
            if not isinstance(key, dict):
                continue

            columns = key.get("columns")

            if not isinstance(columns, list):
                continue

            unknown = [c for c in columns if c not in known]

            if unknown:
                issues.append(
                    Issue(
                        f"{rel}::grain.keys[{i}]",
                        "annotations.grain-unknown-column",
                        "warning",
                        f"statistics.annotations.yaml's grain names column(s) {unknown!r}, "
                        "which statistics.yaml does not have.",
                        "§2.7.1",
                    ),
                )

    return issues


def check_claims(
    print_root: Path,
    manifest_data: dict,
    *,
    on_table: TableSink | None = None,
) -> list[Issue]:
    """Warn when a checkable annotation claim contradicts its column's own statistic.

    Needs both sibling artifacts. A view's catalog-only columns (SPEC 2.2.15) emit no measured
    stat, so a claim against one resolves `annotations.claim-unassertable`. The axis is
    advisory (SPEC 2.4), so every finding is a warning.
    """

    issues: list[Issue] = []
    fields = _annotated_columns(print_root, manifest_data, "claims", dict, on_table)

    for rel, col_name, claims, col_stats, scope in fields:
        for stat, raw in claims.items():
            issues.extend(_check_claim(rel, col_name, stat, raw, col_stats, scope))

    return issues


def _check_claim(
    rel: str,
    col_name: str,
    stat: str,
    raw: Any,
    col_stats: dict[str, Any],
    scope: ScanScope | None = None,
) -> list[Issue]:
    """Evaluate one `claims` predicate; emit at most one Issue.

    A claim is the `<stat>: <predicate>` shape ASSERTIONS.md section 2 specifies for
    `.dbprint.yaml`, scoped to its column, so the DSL's own parser and evaluator are reused.
    """

    path = f"{rel}::columns.{col_name}.claims.{stat}"

    if not isinstance(stat, str) or not is_assertable_stat(stat):
        return [_unassertable(path, f"{stat!r} is not a checkable stat")]

    if is_value_bearing_stat(stat) and is_redacted(col_stats):
        marker = read_column_field(col_stats, "redacted").value

        return [_unassertable(path, f"column is redacted ({marker!r})")]

    predicate = parse_predicate(stat, raw)

    if isinstance(predicate, MalformedPredicate):
        return [_unassertable(path, predicate.reason)]

    ref = resolve_stat(col_stats, stat, scope)
    inapplicable = inapplicable_reason(col_stats, col_name, stat, predicate, ref)

    if inapplicable is not None:
        return [_unassertable(path, inapplicable)]

    outcome = eval_predicate(predicate, ref.value)

    if outcome.passed:
        return []

    if outcome.malformed:
        return [_unassertable(path, outcome.detail)]

    detail = with_evidence(col_stats, stat, outcome.detail)

    return [
        Issue(
            path,
            "annotations.claim-contradicts-statistic",
            "warning",
            f"claims.{stat}={raw!r} contradicts the measured value: {detail}",
            "§2.7.1",
        ),
    ]


def _unassertable(path: str, reason: str) -> Issue:
    return Issue(path, "annotations.claim-unassertable", "warning", reason, "§2.7.1")


def check_value_notes(
    print_root: Path,
    manifest_data: dict,
    *,
    on_table: TableSink | None = None,
) -> list[Issue]:
    """Cross-check value-grain notes against the column's own published values.

    A note is stale only under an exhaustive `values` list (`values_coverage == 1.0`); a
    truncated list may hold the value unlisted, and a redacted column has no literal to
    check against at all.
    """

    issues: list[Issue] = []
    fields = _annotated_columns(print_root, manifest_data, "values", list, on_table)

    for rel, col_name, values, col_stats, scope in fields:
        issues.extend(_check_value_notes(rel, col_name, values, col_stats, scope))

    return issues


def _check_value_notes(
    rel: str,
    col_name: str,
    values: list[Any],
    col_stats: dict[str, Any],
    scope: ScanScope | None = None,
) -> list[Issue]:
    issues: list[Issue] = []

    if is_redacted(col_stats):
        for i, entry in enumerate(values):
            if isinstance(entry, dict):
                issues.append(_value_unassertable(rel, col_name, i, "column is redacted"))

        return issues

    if not list_is_table_domain(col_stats, scope):
        return issues

    published = {
        entry.get("value")
        for entry in (column_value(col_stats, "values") or [])
        if isinstance(entry, dict)
    }

    for i, entry in enumerate(values):
        if not isinstance(entry, dict) or entry.get("value") in published:
            continue

        issues.append(
            Issue(
                f"{rel}::columns.{col_name}.values[{i}]",
                "annotations.unknown-value",
                "warning",
                f"note names value {entry.get('value')!r}, which statistics.yaml's "
                "exhaustive values list does not have.",
                "§2.7.1",
            ),
        )

    return issues


def _value_unassertable(rel: str, col_name: str, i: int, reason: str) -> Issue:
    return Issue(
        f"{rel}::columns.{col_name}.values[{i}]",
        "annotations.value-note-unassertable",
        "warning",
        reason,
        "§2.7.1",
    )


def _annotated_columns(
    print_root: Path,
    manifest_data: dict,
    field: str,
    kind: type,
    on_table: TableSink | None,
) -> Iterator[tuple[str, str, Any, dict[str, Any], ScanScope | None]]:
    tables = annotated_tables(
        print_root,
        manifest_data,
        "statistics_annotations",
        "statistics",
        on_table,
    )

    for ann_path, ann_data, stats_data in tables:
        columns = ann_data.get("columns")
        stats_columns = stats_data.get("columns")

        if not isinstance(columns, dict) or not isinstance(stats_columns, dict):
            continue

        rel = str(ann_path.relative_to(print_root))

        for col_name, entry in columns.items():
            if not isinstance(entry, dict) or not isinstance(entry.get(field), kind):
                continue

            col_stats = stats_columns.get(col_name)

            if isinstance(col_stats, dict):
                yield rel, col_name, entry[field], col_stats, scope_of(stats_data)
