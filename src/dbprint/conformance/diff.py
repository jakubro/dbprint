"""Diff invariants per SPEC 2.6."""

from __future__ import annotations

from collections import Counter
from typing import Any, TypeGuard

from .issue import Issue


_NON_NUMERIC_STATS = {
    "distribution",
    "classification",
    "values",
}

_KIND_TO_SUMMARY_KEY: dict[str, str] = {
    "table_added": "tables_added",
    "table_removed": "tables_removed",
    "column_added": "columns_added",
    "column_removed": "columns_removed",
    "column_type_changed": "columns_type_changed",
    "column_nullable_changed": "columns_nullable_changed",
    "column_default_changed": "columns_default_changed",
    "statistic_changed": "statistics_drifted",
}

# Ordered, because the mismatch message names them in this order.
_TABLE_TOTAL_KEYS = (
    "tables_modified",
    "unchanged_tables",
    "unevaluated_tables",
    "tables_added",
)

_GROUP_KINDS: dict[str, set[str]] = {
    "relationships_changed": {
        "relationship_added",
        "relationship_removed",
        "relationship_modified",
    },
    "indexes_changed": {"index_added", "index_removed", "index_modified"},
    "comments_changed": {"comment_changed"},
}


def check(data: Any, path: str) -> list[Issue]:
    """Check one diff.yaml body beyond what the JSON Schema covers."""

    if not isinstance(data, dict):
        return []

    issues: list[Issue] = []
    changes = data.get("changes", []) or []
    summary = data.get("summary", {}) or {}

    issues.extend(_check_summary_counts(changes, summary, path))
    issues.extend(_check_summary_total(data, summary, path))

    for i, change in enumerate(changes):
        if not isinstance(change, dict):
            continue

        where = f"{path}::changes[{i}]"
        kind = change.get("kind")

        if kind == "relationship_modified":
            issues.extend(_check_rel_modified(change, where))
        elif kind == "comment_changed":
            issues.extend(_check_comment(change, where))
        elif kind == "statistic_changed":
            issues.extend(_check_statistic(change, where))
        elif kind == "table_row_count_changed":
            issues.extend(_check_row_count_changed(change, where))
        elif kind == "grain_changed":
            issues.extend(_check_grain_changed(change, where))
        elif kind == "physical_layout_changed":
            issues.extend(_check_physical_layout_changed(change, where))

    return issues


def _check_summary_counts(changes: list[Any], summary: dict, path: str) -> list[Issue]:
    actual: Counter[str] = Counter()

    for change in changes:
        if not isinstance(change, dict):
            continue

        kind = change.get("kind")

        if isinstance(kind, str):
            actual[kind] += 1

    mismatches: list[str] = []

    for kind, summary_key in _KIND_TO_SUMMARY_KEY.items():
        if summary.get(summary_key, 0) != actual[kind]:
            mismatches.append(
                f"{summary_key} reports {summary.get(summary_key)}, actual events for {kind}: {actual[kind]}",
            )

    for summary_key, kinds in _GROUP_KINDS.items():
        expected = sum(actual[k] for k in kinds)

        if summary.get(summary_key, 0) != expected:
            mismatches.append(
                f"{summary_key} reports {summary.get(summary_key)}, actual events sum: {expected}",
            )

    if not mismatches:
        return []

    return [
        Issue(
            path,
            "diff.summary-count-mismatch",
            "warning",
            "; ".join(mismatches),
            "§2.6.4",
        ),
    ]


def _check_summary_total(data: dict, summary: dict, path: str) -> list[Issue]:
    """The four table counters partition `target.tables_scanned` (SPEC 2.6.4).

    A count that silently absorbs objects the diff never compared shows up here as arithmetic.
    """

    target = data.get("target")
    scanned = target.get("tables_scanned") if isinstance(target, dict) else None

    if not _is_count(scanned):
        return []

    counted: list[int] = []

    # A missing or misshapen operand already reads as `schema.missing-required-field`.
    for key in _TABLE_TOTAL_KEYS:
        value = summary.get(key)

        if not _is_count(value):
            return []

        counted.append(value)

    total = sum(counted)

    if total == scanned:
        return []

    parts = ", ".join(
        f"{key}={value}" for key, value in zip(_TABLE_TOTAL_KEYS, counted, strict=True)
    )

    return [
        Issue(
            path,
            "diff.summary-total-mismatch",
            "warning",
            f"{parts} sum to {total}, but target.tables_scanned is {scanned}.",
            "§2.6.4",
        ),
    ]


def _check_rel_modified(change: dict, where: str) -> list[Issue]:
    if "on_delete" not in change and "on_update" not in change:
        return [
            Issue(
                where,
                "diff.relationship-modified-no-change",
                "error",
                "relationship_modified event must carry on_delete and/or on_update.",
                "§2.6.6",
            ),
        ]

    return []


def _check_comment(change: dict, where: str) -> list[Issue]:
    target = change.get("target")
    has_column = "column" in change

    if target == "column" and not has_column:
        return [
            Issue(
                where,
                "diff.comment-target-column-mismatch",
                "error",
                "comment_changed with target=column requires the `column` field.",
                "§2.6.6",
            ),
        ]
    elif target == "table" and has_column:
        return [
            Issue(
                where,
                "diff.comment-target-column-mismatch",
                "error",
                "comment_changed with target=table MUST NOT carry the `column` field.",
                "§2.6.6",
            ),
        ]
    else:
        return []


def _check_statistic(change: dict, where: str) -> list[Issue]:
    stat = change.get("stat", "")

    if not isinstance(stat, str):
        return []

    is_non_numeric = stat in _NON_NUMERIC_STATS or stat.endswith(".classification")

    if is_non_numeric and ("delta" in change or "delta_pct" in change):
        return [
            Issue(
                where,
                "diff.statistic-changed-delta-on-non-numeric",
                "error",
                f"statistic_changed for {stat!r} (non-numeric) MUST NOT carry delta / delta_pct.",
                "§2.6.6",
            ),
        ]

    return _check_delta_sign_agreement(change, where)


def _check_delta_sign_agreement(change: dict, where: str) -> list[Issue]:
    delta = change.get("delta")
    delta_pct = change.get("delta_pct")

    if not isinstance(delta, (int, float)) or not isinstance(delta_pct, (int, float)):
        return []

    if _sign(delta) == _sign(delta_pct):
        return []

    return [
        Issue(
            where,
            "diff.statistic-changed-delta-pct-sign-mismatch",
            "error",
            f"delta={delta!r} and delta_pct={delta_pct!r} disagree in sign.",
            "§2.6.6",
        ),
    ]


def _check_grain_changed(change: dict, where: str) -> list[Issue]:
    if change.get("before") != change.get("after"):
        return []

    return [
        Issue(
            where,
            "diff.grain-changed-no-change",
            "error",
            "grain_changed event's before and after are identical.",
            "§2.6.6",
        ),
    ]


def _check_physical_layout_changed(change: dict, where: str) -> list[Issue]:
    if change.get("before") != change.get("after"):
        return []

    return [
        Issue(
            where,
            "diff.physical-layout-changed-no-change",
            "error",
            "physical_layout_changed event's before and after are identical.",
            "§2.6.6",
        ),
    ]


def _is_count(value: Any) -> TypeGuard[int]:
    """A non-negative integer that is not a bool, which `isinstance` would admit."""

    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


def _check_row_count_changed(change: dict, where: str) -> list[Issue]:
    before, after, delta = change.get("before"), change.get("after"), change.get("delta")

    if not isinstance(before, int) or not isinstance(after, int) or not isinstance(delta, int):
        return []

    if delta == after - before:
        return []

    return [
        Issue(
            where,
            "diff.row-count-changed-delta-mismatch",
            "error",
            f"delta={delta!r} does not equal after ({after!r}) - before ({before!r}).",
            "§2.6.6",
        ),
    ]
