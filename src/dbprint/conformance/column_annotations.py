"""statistics.annotations.yaml invariants per SPEC 2.7.1."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.assertions.predicate import (
    MalformedPredicate,
    is_assertable_stat,
    is_value_bearing_stat,
)
from dbprint.assertions.predicate import evaluate as eval_predicate
from dbprint.assertions.predicate import parse as parse_predicate
from dbprint.assertions.predicate import resolve as resolve_stat
from .issue import Issue
from .layout import declared_artifacts, walkable_tables
from .progress import TableSink
from .yaml_utils import load_yaml


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
    tables = walkable_tables(manifest_data)
    total = len(tables)

    for i, (tbl_fqn, tbl_entry) in enumerate(tables.items(), start=1):
        if on_table is not None:
            on_table(tbl_fqn, i, total)

        artifacts = declared_artifacts(tbl_entry)

        if "statistics_annotations" not in artifacts or "statistics" not in artifacts:
            continue

        tbl_dir = print_root / tbl_entry.get("path", "")
        ann_path = tbl_dir / artifacts["statistics_annotations"]
        stats_path = tbl_dir / artifacts["statistics"]

        if not ann_path.is_file() or not stats_path.is_file():
            continue

        try:
            ann_data = load_yaml(ann_path)
            stats_data = load_yaml(stats_path)
        except yaml.YAMLError:
            continue

        if not isinstance(ann_data, dict) or not isinstance(stats_data, dict):
            continue

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
    tables = walkable_tables(manifest_data)
    total = len(tables)

    for i, (tbl_fqn, tbl_entry) in enumerate(tables.items(), start=1):
        if on_table is not None:
            on_table(tbl_fqn, i, total)

        artifacts = declared_artifacts(tbl_entry)

        if "statistics_annotations" not in artifacts or "statistics" not in artifacts:
            continue

        tbl_dir = print_root / tbl_entry.get("path", "")
        ann_path = tbl_dir / artifacts["statistics_annotations"]
        stats_path = tbl_dir / artifacts["statistics"]

        if not ann_path.is_file() or not stats_path.is_file():
            continue

        try:
            ann_data = load_yaml(ann_path)
            stats_data = load_yaml(stats_path)
        except yaml.YAMLError:
            continue

        if not isinstance(ann_data, dict) or not isinstance(stats_data, dict):
            continue

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
    tables = walkable_tables(manifest_data)
    total = len(tables)

    for i, (tbl_fqn, tbl_entry) in enumerate(tables.items(), start=1):
        if on_table is not None:
            on_table(tbl_fqn, i, total)

        artifacts = declared_artifacts(tbl_entry)

        if "statistics_annotations" not in artifacts or "statistics" not in artifacts:
            continue

        tbl_dir = print_root / tbl_entry.get("path", "")
        ann_path = tbl_dir / artifacts["statistics_annotations"]
        stats_path = tbl_dir / artifacts["statistics"]

        if not ann_path.is_file() or not stats_path.is_file():
            continue

        try:
            ann_data = load_yaml(ann_path)
            stats_data = load_yaml(stats_path)
        except yaml.YAMLError:
            continue

        if not isinstance(ann_data, dict) or not isinstance(stats_data, dict):
            continue

        columns = ann_data.get("columns")
        stats_columns = stats_data.get("columns")

        if not isinstance(columns, dict) or not isinstance(stats_columns, dict):
            continue

        rel = str(ann_path.relative_to(print_root))

        for col_name, entry in columns.items():
            if not isinstance(entry, dict):
                continue

            claims = entry.get("claims")

            if not isinstance(claims, dict):
                continue

            col_stats = stats_columns.get(col_name)

            if not isinstance(col_stats, dict):
                continue

            for stat, raw in claims.items():
                issues.extend(_check_claim(rel, col_name, stat, raw, col_stats))

    return issues


def _check_claim(
    rel: str,
    col_name: str,
    stat: str,
    raw: Any,
    col_stats: dict[str, Any],
) -> list[Issue]:
    """Evaluate one `claims` predicate; emit at most one Issue.

    A claim is the `<stat>: <predicate>` shape ASSERTIONS.md section 2 specifies for
    `.dbprint.yaml`, scoped to its column, so the DSL's own parser and evaluator are reused.
    """

    path = f"{rel}::columns.{col_name}.claims.{stat}"

    if not isinstance(stat, str) or not is_assertable_stat(stat):
        return [_unassertable(path, f"{stat!r} is not a checkable stat")]

    if is_value_bearing_stat(stat) and col_stats.get("redacted") is not None:
        return [_unassertable(path, f"column is redacted ({col_stats['redacted']!r})")]

    predicate = parse_predicate(stat, raw)

    if isinstance(predicate, MalformedPredicate):
        return [_unassertable(path, predicate.reason)]

    ref = resolve_stat(col_stats, stat)

    if not ref.found:
        return [_unassertable(path, f"{stat!r} not emitted for column {col_name!r}")]

    outcome = eval_predicate(predicate, ref.value)

    if outcome.passed:
        return []

    return [
        Issue(
            path,
            "annotations.claim-contradicts-statistic",
            "warning",
            f"claims.{stat}={raw!r} contradicts the measured value: {outcome.detail}",
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
    tables = walkable_tables(manifest_data)
    total = len(tables)

    for i, (tbl_fqn, tbl_entry) in enumerate(tables.items(), start=1):
        if on_table is not None:
            on_table(tbl_fqn, i, total)

        artifacts = declared_artifacts(tbl_entry)

        if "statistics_annotations" not in artifacts or "statistics" not in artifacts:
            continue

        tbl_dir = print_root / tbl_entry.get("path", "")
        ann_path = tbl_dir / artifacts["statistics_annotations"]
        stats_path = tbl_dir / artifacts["statistics"]

        if not ann_path.is_file() or not stats_path.is_file():
            continue

        try:
            ann_data = load_yaml(ann_path)
            stats_data = load_yaml(stats_path)
        except yaml.YAMLError:
            continue

        if not isinstance(ann_data, dict) or not isinstance(stats_data, dict):
            continue

        columns = ann_data.get("columns")
        stats_columns = stats_data.get("columns")

        if not isinstance(columns, dict) or not isinstance(stats_columns, dict):
            continue

        rel = str(ann_path.relative_to(print_root))

        for col_name, entry in columns.items():
            if not isinstance(entry, dict):
                continue

            values = entry.get("values")

            if not isinstance(values, list):
                continue

            col_stats = stats_columns.get(col_name)

            if not isinstance(col_stats, dict):
                continue

            issues.extend(_check_value_notes(rel, col_name, values, col_stats))

    return issues


def _check_value_notes(
    rel: str,
    col_name: str,
    values: list[Any],
    col_stats: dict[str, Any],
) -> list[Issue]:
    issues: list[Issue] = []

    if col_stats.get("redacted") is not None:
        for i, entry in enumerate(values):
            if isinstance(entry, dict):
                issues.append(_value_unassertable(rel, col_name, i, "column is redacted"))

        return issues

    coverage = col_stats.get("values_coverage")
    exhaustive = (
        isinstance(coverage, (int, float)) and not isinstance(coverage, bool) and coverage == 1.0
    )

    if not exhaustive:
        return issues

    published = {
        entry.get("value") for entry in (col_stats.get("values") or []) if isinstance(entry, dict)
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
