"""Relationships invariants per SPEC 2.3."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.spec.classification import compute_cardinality_ratio
from dbprint.spec.sketch import K as SKETCH_K
from dbprint.spec.sketch import (
    answerable_count,
    answerable_subset_containment,
    decode_sketch,
    estimate_intersection,
)
from .issue import Issue
from .layout import declared_artifacts, walkable_tables
from .yaml_utils import load_yaml


def check_entry(data: Any, path: str, tbl_fqn: str) -> list[Issue]:
    """Check one relationships.yaml body beyond what the JSON Schema covers."""

    if not isinstance(data, dict):
        return []

    issues: list[Issue] = []

    for section in ("refers_to", "referenced_by"):
        for i, entry in enumerate(data.get(section, []) or []):
            if not isinstance(entry, dict):
                continue

            where = f"{path}::{section}[{i}]"
            issues.extend(_check_array_length(entry, where, section))

            if section == "refers_to":
                issues.extend(check_path_endpoint(entry, where))

    issues.extend(_check_ineligible_target(data, path))

    return issues


def check_path_endpoint(entry: dict[str, Any], where: str) -> list[Issue]:
    """SPEC 2.3.9: `path`/`target_path` are legal only on a single-column endpoint.

    Shared with `relationship_annotations.py`: an authored edge (SPEC 2.7.2) carries the same
    shape and is bound by the same rule.
    """

    issues: list[Issue] = []
    issues.extend(_check_one_path(entry, where, "path", "column"))
    issues.extend(_check_one_path(entry, where, "target_path", "target_column"))

    return issues


def _check_one_path(
    entry: dict[str, Any],
    where: str,
    path_key: str,
    partner_key: str,
) -> list[Issue]:
    partner = entry.get(partner_key)

    if entry.get(path_key) is None or not isinstance(partner, list) or len(partner) == 1:
        return []

    return [
        Issue(
            where,
            "relationships.path-on-composite-endpoint",
            "error",
            f"{path_key} is present but {partner_key} has {len(partner)} entries; "
            "a path endpoint is legal only on a single-column endpoint.",
            "§2.3.9",
        ),
    ]


def _check_ineligible_target(data: dict, path: str) -> list[Issue]:
    """SPEC 2.3.8: naming inference cannot resolve an edge to an ineligible object.

    `eligible_target` covers single-column naming-inference eligibility, so only an INFERRED
    entry contradicts `false`: a declared edge may still target a composite key inference misses.
    """

    if data.get("eligible_target") is not False:
        return []

    referenced_by = data.get("referenced_by")

    if not isinstance(referenced_by, list):
        return []

    inferred = sum(
        1 for e in referenced_by if isinstance(e, dict) and e.get("detection") == "inferred"
    )

    if not inferred:
        return []

    return [
        Issue(
            f"{path}::referenced_by",
            "relationships.ineligible-target-is-referenced",
            "error",
            f"eligible_target is false but referenced_by carries {inferred} inferred "
            "entries; naming inference cannot resolve an edge to an ineligible object.",
            "§2.3.8",
        ),
    ]


def check_reciprocity(print_root: Path, manifest_data: dict) -> list[Issue]:
    """Verify that every referenced_by entry has a matching refers_to in the source table."""

    issues: list[Issue] = []
    refers_index: dict[str, list[tuple[str, list, list]]] = {}
    referenced_index: dict[str, list[tuple[str, str, list, list]]] = {}

    for tbl_fqn, tbl_entry in walkable_tables(manifest_data).items():
        artifacts = declared_artifacts(tbl_entry)

        if "relationships" not in artifacts:
            continue

        rel_path = print_root / tbl_entry.get("path", "") / artifacts["relationships"]

        if not rel_path.is_file():
            continue

        try:
            data = load_yaml(rel_path)
        except yaml.YAMLError:
            continue

        if not isinstance(data, dict):
            continue

        for entry in data.get("refers_to", []) or []:
            if not isinstance(entry, dict):
                continue

            refers_index.setdefault(tbl_fqn, []).append(
                (
                    entry.get("target_table", ""),
                    list(entry.get("column", [])),
                    list(entry.get("target_column", [])),
                ),
            )

        for entry in data.get("referenced_by", []) or []:
            if not isinstance(entry, dict):
                continue

            referenced_index.setdefault(tbl_fqn, []).append(
                (
                    str(rel_path.relative_to(print_root)),
                    entry.get("referencer_table", ""),
                    list(entry.get("referencer_column", [])),
                    list(entry.get("column", [])),
                ),
            )

    for tbl_fqn, rb_entries in referenced_index.items():
        for rel_path_str, referencer_table, referencer_column, this_column in rb_entries:
            # Only an in-manifest referencer missing its half is an error (SPEC 2.3.6).
            if referencer_table not in manifest_data.get("tables", {}):
                continue

            outgoing = refers_index.get(referencer_table, [])
            match = any(
                target == tbl_fqn and ref_col == referencer_column and tgt_col == this_column
                for target, ref_col, tgt_col in outgoing
            )

            if not match:
                issues.append(
                    Issue(
                        rel_path_str,
                        "relationships.broken-reciprocity",
                        "error",
                        f"referenced_by entry from {referencer_table} has no matching refers_to in its source table.",
                        "§2.3.3",
                    ),
                )

    return issues


def check_observed_arithmetic(print_root: Path, manifest_data: dict) -> list[Issue]:
    """SPEC 2.3.10: an `observed` block must recompute to what its two endpoints measured.

    Walks `refers_to` only, since `referenced_by`'s mirror entry states the same physical
    edge with the same numbers. Needs both endpoints' `statistics.yaml`, so it runs as its
    own pass rather than inside the per-artifact `_check_artifact` dispatch.
    """

    issues: list[Issue] = []
    tables = walkable_tables(manifest_data)

    for tbl_entry in tables.values():
        artifacts = declared_artifacts(tbl_entry)

        if "relationships" not in artifacts or "statistics" not in artifacts:
            continue

        tbl_dir = print_root / tbl_entry.get("path", "")
        rel_path = tbl_dir / artifacts["relationships"]
        source_stats = _load_mapping(tbl_dir / artifacts["statistics"])

        if source_stats is None or not rel_path.is_file():
            continue

        try:
            rel_data = load_yaml(rel_path)
        except yaml.YAMLError:
            continue

        if not isinstance(rel_data, dict):
            continue

        rel = str(rel_path.relative_to(print_root))

        for i, entry in enumerate(rel_data.get("refers_to", []) or []):
            if not isinstance(entry, dict) or not isinstance(entry.get("observed"), dict):
                continue

            issues.extend(
                _check_observed_entry(
                    entry["observed"],
                    entry,
                    source_stats,
                    tables,
                    print_root,
                    f"{rel}::refers_to[{i}].observed",
                ),
            )

    return issues


def _load_mapping(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None

    try:
        data = load_yaml(path)
    except yaml.YAMLError:
        return None

    return data if isinstance(data, dict) else None


def _decode_sketch_field(sketch: Any) -> list[int] | None:
    """A column's decoded `sketch.values` (SPEC 2.2.14), or None when absent/malformed.

    Malformed decodes to None rather than raising: statistics.py reports the broken sketch,
    and this pass only needs to know whether a trustworthy one exists to recompute against.
    """

    if not isinstance(sketch, dict) or not isinstance(sketch.get("values"), str):
        return None

    return decode_sketch(sketch["values"])


def _check_observed_entry(
    observed: dict[str, Any],
    entry: dict[str, Any],
    source_stats: dict[str, Any],
    tables: dict[str, dict[str, Any]],
    print_root: Path,
    where: str,
) -> list[Issue]:
    if observed.get("scope_compatible") is False:
        return []  # no ratio fields to recompute; shape alone is the schema's job

    column = entry.get("column")
    target_column = entry.get("target_column")

    if not isinstance(column, list) or len(column) != 1:
        return []

    if not isinstance(target_column, list) or len(target_column) != 1:
        return []

    src_col = (source_stats.get("columns") or {}).get(column[0])

    if not isinstance(src_col, dict):
        return []

    target_entry = tables.get(entry.get("target_table", ""))

    if target_entry is None:
        return []

    target_artifacts = declared_artifacts(target_entry)

    if "statistics" not in target_artifacts:
        return []

    target_stats = _load_mapping(
        print_root / target_entry.get("path", "") / target_artifacts["statistics"],
    )

    if target_stats is None:
        return []

    tgt_col = (target_stats.get("columns") or {}).get(target_column[0])

    if not isinstance(tgt_col, dict):
        return []

    src_cardinality = src_col.get("cardinality")
    src_row_count = source_stats.get("row_count")
    tgt_cardinality = tgt_col.get("cardinality")

    if (
        not isinstance(src_cardinality, int)
        or not src_cardinality
        or not isinstance(src_row_count, int)
        or not isinstance(tgt_cardinality, int)
        or not tgt_cardinality
    ):
        return []

    issues: list[Issue] = []
    expected_fanout = round(src_row_count / src_cardinality, 6)

    if observed.get("fanout_avg") != expected_fanout:
        issues.append(
            Issue(
                where,
                "relationships.observed-fanout-mismatch",
                "error",
                f"fanout_avg={observed.get('fanout_avg')!r} does not match "
                f"row_count/cardinality={expected_fanout!r} from the referencing column's "
                "own statistics.",
                "§2.3.10",
            ),
        )

    src_sketch = _decode_sketch_field(src_col.get("sketch"))
    tgt_sketch = _decode_sketch_field(tgt_col.get("sketch"))

    if src_sketch is not None and tgt_sketch is not None:
        # With a sketch on both endpoints, target_coverage is sketch-measured and containment
        # exists only here (SPEC 2.3.10). An exhaustive child reads its own answerable subset
        # (SPEC 2.2.14); recomputing through the producer's scale-up would compare it against
        # itself, so operands are read here instead - a producer bug stays visible.
        if len(src_sketch) < SKETCH_K:
            result = answerable_subset_containment(src_sketch, tgt_sketch)

            if result is None:
                # No answerable evidence either way - target_coverage falls back to the
                # cardinality-derived baseline (SPEC 2.3.10's else arm), containment absent.
                expected_containment = None
                expected_answerable_count = None
                expected_coverage = compute_cardinality_ratio(src_cardinality, tgt_cardinality)
            else:
                ratio, expected_answerable_count = result
                expected_containment = min(1.0, round(ratio, 6))
                expected_coverage = min(
                    1.0,
                    round(round(ratio * src_cardinality) / tgt_cardinality, 6),
                )
        else:
            intersection = estimate_intersection(src_sketch, tgt_sketch)
            expected_coverage = min(1.0, round(intersection / tgt_cardinality, 6))
            expected_containment = min(1.0, round(intersection / src_cardinality, 6))
            expected_answerable_count = answerable_count(src_sketch, tgt_sketch)

        if observed.get("target_coverage") != expected_coverage:
            issues.append(
                Issue(
                    where,
                    "relationships.observed-coverage-mismatch",
                    "error",
                    f"target_coverage={observed.get('target_coverage')!r} does not match "
                    f"the sketch-measured estimate {expected_coverage!r} from the two "
                    "endpoints' own sketches.",
                    "§2.3.10",
                ),
            )

        if expected_containment is None:
            # SPEC 2.3.10: an empty answerable subset licenses no containment, exact or
            # estimated - publishing one is a defect on its own footing, not a mismatch.
            if observed.get("containment") is not None:
                issues.append(
                    Issue(
                        where,
                        "relationships.observed-containment-forbidden",
                        "error",
                        f"containment={observed.get('containment')!r} is published, but the "
                        "answerable subset between the two endpoints' sketches is empty - "
                        "§2.3.10 forbids the field here.",
                        "§2.3.10",
                    ),
                )
        elif observed.get("containment") != expected_containment:
            issues.append(
                Issue(
                    where,
                    "relationships.observed-containment-mismatch",
                    "error",
                    f"containment={observed.get('containment')!r} does not match the "
                    f"sketch-measured estimate {expected_containment!r} from the two "
                    "endpoints' own sketches.",
                    "§2.3.10",
                ),
            )

        if observed.get("answerable_count") != expected_answerable_count:
            if expected_answerable_count is None:
                detail = (
                    f"answerable_count={observed.get('answerable_count')!r} is published, "
                    "but the answerable subset between the two endpoints' sketches is "
                    "empty - §2.3.10 forbids the field here."
                )
            else:
                detail = (
                    f"answerable_count={observed.get('answerable_count')!r} does not match "
                    f"{expected_answerable_count!r}, the count of the referencing column's "
                    "own retained hashes below the shared threshold (§2.2.14)."
                )

            issues.append(
                Issue(
                    where,
                    "relationships.observed-answerable-count-mismatch",
                    "error",
                    detail,
                    "§2.3.10",
                ),
            )
    else:
        expected_coverage = compute_cardinality_ratio(src_cardinality, tgt_cardinality)

        if observed.get("target_coverage") != expected_coverage:
            issues.append(
                Issue(
                    where,
                    "relationships.observed-coverage-mismatch",
                    "error",
                    f"target_coverage={observed.get('target_coverage')!r} does not match "
                    f"the cardinality ratio {expected_coverage!r} from the two endpoints' "
                    "own statistics.",
                    "§2.3.10",
                ),
            )

    if "coherent" in observed:
        expected_coherent = src_cardinality <= tgt_cardinality

        if observed.get("coherent") != expected_coherent:
            issues.append(
                Issue(
                    where,
                    "relationships.observed-coherent-mismatch",
                    "error",
                    f"coherent={observed.get('coherent')!r} but the referencing column's "
                    f"cardinality ({src_cardinality}) "
                    f"{'exceeds' if src_cardinality > tgt_cardinality else 'does not exceed'} "
                    f"the referenced column's ({tgt_cardinality}).",
                    "§2.3.10",
                ),
            )

    return issues


def _check_array_length(entry: dict, where: str, section: str) -> list[Issue]:
    col = entry.get("column", [])

    if section == "refers_to":
        partner_key = "target_column"
    else:
        partner_key = "referencer_column"

    partner = entry.get(partner_key, [])

    if isinstance(col, list) and isinstance(partner, list) and len(col) != len(partner):
        return [
            Issue(
                where,
                "relationships.column-array-length-mismatch",
                "error",
                f"len(column)={len(col)} differs from len({partner_key})={len(partner)}; arrays must match.",
                "§2.3.4",
            ),
        ]

    return []
