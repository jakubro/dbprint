"""relationships.annotations.yaml invariants per SPEC 2.7.2."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.assertions.predicate import MalformedPredicate, is_assertable_edge_stat
from dbprint.assertions.predicate import evaluate as eval_predicate
from dbprint.assertions.predicate import parse as parse_predicate
from dbprint.assertions.predicate import resolve as resolve_stat
from .issue import Issue
from .layout import declared_artifacts, walkable_tables
from .progress import TableSink
from .relationships import check_path_endpoint
from .yaml_utils import load_yaml


def check_entry(data: Any, path: str, tbl_fqn: str) -> list[Issue]:
    """Check one relationships.annotations.yaml body beyond what the JSON Schema covers.

    A `refers_to` entry shares `relationships.yaml`'s shape (SPEC 2.7.2's layering rule), so
    the path-endpoint rule of SPEC 2.3.9 binds it too.
    """

    del tbl_fqn

    if not isinstance(data, dict):
        return []

    issues: list[Issue] = []

    for i, entry in enumerate(data.get("refers_to", []) or []):
        if isinstance(entry, dict):
            issues.extend(check_path_endpoint(entry, f"{path}::refers_to[{i}]"))

    return issues


def check_verdicts(
    print_root: Path,
    manifest_data: dict,
    *,
    on_table: TableSink | None = None,
) -> list[Issue]:
    """Cross-check each annotated edge against its source table's own relationships.yaml.

    An entry addresses an edge by (column, target_table, target_column): a `verdict` on an
    edge `refers_to` omits is stale, and one on a `declared` edge contradicts a measurement
    (SPEC 2.4's precedence rule). An entry with no `verdict` is a human addition, never stale.
    """

    issues: list[Issue] = []
    tables = walkable_tables(manifest_data)
    total = len(tables)

    for i, (tbl_fqn, tbl_entry) in enumerate(tables.items(), start=1):
        if on_table is not None:
            on_table(tbl_fqn, i, total)

        artifacts = declared_artifacts(tbl_entry)

        if "relationships_annotations" not in artifacts or "relationships" not in artifacts:
            continue

        tbl_dir = print_root / tbl_entry.get("path", "")
        ann_path = tbl_dir / artifacts["relationships_annotations"]
        rel_path = tbl_dir / artifacts["relationships"]

        if not ann_path.is_file() or not rel_path.is_file():
            continue

        try:
            ann_data = load_yaml(ann_path)
            rel_data = load_yaml(rel_path)
        except yaml.YAMLError:
            continue

        if not isinstance(ann_data, dict) or not isinstance(rel_data, dict):
            continue

        entries = ann_data.get("refers_to")

        if not isinstance(entries, list):
            continue

        emitted = _emitted_edges(rel_data)
        rel = str(ann_path.relative_to(print_root))

        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue

            issues.extend(_check_one(entry, emitted, f"{rel}::refers_to[{i}]"))

    return issues


def check_claims(
    print_root: Path,
    manifest_data: dict,
    *,
    on_table: TableSink | None = None,
) -> list[Issue]:
    """Warn when a checkable edge claim (SPEC 2.7.2) contradicts its edge's own `observed`.

    Mirrors `column_annotations.check_claims` against `relationships.yaml`, differing only in
    vocabulary (`EDGE_ASSERTABLE_STATS`) and addressing (a (column, target_table,
    target_column) triplet). An edge missing `observed` resolves every claim unassertable
    rather than contradicted, and carries no `verdict`, so no staleness check runs.
    """

    issues: list[Issue] = []
    tables = walkable_tables(manifest_data)
    total = len(tables)

    for i, (tbl_fqn, tbl_entry) in enumerate(tables.items(), start=1):
        if on_table is not None:
            on_table(tbl_fqn, i, total)

        artifacts = declared_artifacts(tbl_entry)

        if "relationships_annotations" not in artifacts or "relationships" not in artifacts:
            continue

        tbl_dir = print_root / tbl_entry.get("path", "")
        ann_path = tbl_dir / artifacts["relationships_annotations"]
        rel_path = tbl_dir / artifacts["relationships"]

        if not ann_path.is_file() or not rel_path.is_file():
            continue

        try:
            ann_data = load_yaml(ann_path)
            rel_data = load_yaml(rel_path)
        except yaml.YAMLError:
            continue

        if not isinstance(ann_data, dict) or not isinstance(rel_data, dict):
            continue

        entries = ann_data.get("refers_to")

        if not isinstance(entries, list):
            continue

        emitted = _emitted_edge_entries(rel_data)
        rel = str(ann_path.relative_to(print_root))

        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue

            claims = entry.get("claims")

            if not isinstance(claims, dict):
                continue

            key = _address(entry)
            edge = (emitted.get(key) if key is not None else None) or {}
            path = f"{rel}::refers_to[{i}]"

            for stat, raw in claims.items():
                issues.extend(_check_claim(path, stat, raw, edge))

    return issues


def _check_claim(path: str, stat: str, raw: Any, edge: dict[str, Any]) -> list[Issue]:
    """Evaluate one edge `claims` predicate; emit at most one Issue."""

    claim_path = f"{path}.claims.{stat}"

    if not isinstance(stat, str) or not is_assertable_edge_stat(stat):
        return [_unassertable(claim_path, f"{stat!r} is not a checkable edge stat")]

    predicate = parse_predicate(stat, raw)

    if isinstance(predicate, MalformedPredicate):
        return [_unassertable(claim_path, predicate.reason)]

    ref = resolve_stat(edge, stat)

    if not ref.found:
        return [_unassertable(claim_path, f"{stat!r} not emitted for this edge")]

    outcome = eval_predicate(predicate, ref.value)

    if outcome.passed:
        return []

    return [
        Issue(
            claim_path,
            "annotations.claim-contradicts-statistic",
            "warning",
            f"claims.{stat}={raw!r} contradicts the measured value: {outcome.detail}",
            "§2.7.2",
        ),
    ]


def _unassertable(path: str, reason: str) -> Issue:
    return Issue(path, "annotations.claim-unassertable", "warning", reason, "§2.7.2")


def _emitted_edge_entries(rel_data: dict) -> dict[tuple[Any, ...], dict[str, Any]]:
    """This table's own refers_to entries, keyed by their addressing triplet."""

    out: dict[tuple[Any, ...], dict[str, Any]] = {}

    for entry in rel_data.get("refers_to", []) or []:
        if not isinstance(entry, dict):
            continue

        key = _address(entry)

        if key is not None:
            out[key] = entry

    return out


def _emitted_edges(rel_data: dict) -> dict[tuple[Any, ...], str]:
    """This table's own refers_to entries, keyed by their addressing triplet.

    Values are `detection` - `declared` or `inferred` - which is all a verdict check needs.
    """

    out: dict[tuple[Any, ...], str] = {}

    for entry in rel_data.get("refers_to", []) or []:
        if not isinstance(entry, dict):
            continue

        key = _address(entry)

        if key is not None:
            out[key] = entry.get("detection", "")

    return out


def _address(entry: dict[str, Any]) -> tuple[Any, ...] | None:
    column = entry.get("column")
    target_table = entry.get("target_table")
    target_column = entry.get("target_column")

    if not isinstance(column, list) or not isinstance(target_column, list):
        return None

    return (tuple(column), target_table, tuple(target_column))


def _check_one(
    entry: dict[str, Any],
    emitted: dict[tuple[Any, ...], str],
    where: str,
) -> list[Issue]:
    verdict = entry.get("verdict")

    if verdict is None:
        return []

    key = _address(entry)
    detection = emitted.get(key) if key is not None else None

    if detection is None:
        return [
            Issue(
                where,
                "annotations.unknown-edge",
                "warning",
                "relationships.annotations.yaml carries a verdict for an edge "
                "relationships.yaml does not emit.",
                "§2.7.2",
            ),
        ]

    if detection == "declared":
        return [
            Issue(
                where,
                "annotations.verdict-on-declared-edge",
                "warning",
                "verdict addresses a declared edge; an annotation may correct an "
                "inference, never contradict a measurement (§2.4).",
                "§2.7.2",
            ),
        ]

    return []
