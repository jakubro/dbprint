"""dbprint format v1 conformance suite; see `validate_print` and `Issue`."""

from __future__ import annotations

from pathlib import Path

import yaml

from . import (
    column_annotations,
    ddl,
    diff,
    format_version,
    layout,
    manifest,
    relationship_annotations,
    relationships,
    schema_validation,
    statistics,
)
from .issue import Issue
from .progress import VALIDATION_PASSES, TableSink, ValidationProgress, ValidationTick
from .yaml_utils import load_yaml


__all__ = ["Issue", "ValidationProgress", "ValidationTick", "validate_print"]


def validate_print(
    print_root: Path | str,
    *,
    on_table: ValidationProgress | None = None,
) -> list[Issue]:
    """Validate a dbprint print directory; return ordered list of Issues.

    Conforms iff no issue has `error` severity; ordering is (path, code) for diff stability.
    `on_table` fires per table per pass (SPEC 6.7); only the last pass's tick carries `findings`.
    """

    print_root = Path(print_root)
    issues: list[Issue] = []

    layout_issues, manifest_data = layout.check(print_root)
    issues.extend(layout_issues)

    if manifest_data is None:
        return sorted(issues)

    issues.extend(format_version.check(manifest_data, "manifest.yaml"))
    issues.extend(schema_validation.check_manifest(manifest_data, "manifest.yaml"))

    tables = layout.walkable_tables(manifest_data)
    total = len(tables)
    pass_total = len(VALIDATION_PASSES)
    sinks = [
        _pass_sink(on_table, name, index, pass_total)
        for index, name in enumerate(
            VALIDATION_PASSES,
            start=1,
        )
    ]

    issues.extend(manifest.check(print_root, manifest_data, on_table=sinks[0]))
    issues.extend(manifest.check_manifest_annotations_presence(print_root, manifest_data))

    for i, (tbl_fqn, tbl_entry) in enumerate(tables.items(), start=1):
        if sinks[1] is not None:
            sinks[1](tbl_fqn, i, total)

        tbl_path = tbl_entry.get("path", "")
        tbl_dir = print_root / tbl_path
        artifacts = layout.declared_artifacts(tbl_entry)

        if "statistics" in artifacts:
            issues.extend(
                _check_artifact(
                    print_root,
                    tbl_dir,
                    artifacts["statistics"],
                    tbl_fqn,
                    schema_checker=schema_validation.check_statistics,
                    content_checker=statistics.check,
                ),
            )

        if "relationships" in artifacts:
            issues.extend(
                _check_artifact(
                    print_root,
                    tbl_dir,
                    artifacts["relationships"],
                    tbl_fqn,
                    schema_checker=schema_validation.check_relationships,
                    content_checker=relationships.check_entry,
                ),
            )

        if "statistics_annotations" in artifacts:
            issues.extend(
                _check_artifact(
                    print_root,
                    tbl_dir,
                    artifacts["statistics_annotations"],
                    tbl_fqn,
                    schema_checker=schema_validation.check_statistics_annotations,
                    content_checker=column_annotations.check_entry,
                ),
            )

        if "relationships_annotations" in artifacts:
            issues.extend(
                _check_artifact(
                    print_root,
                    tbl_dir,
                    artifacts["relationships_annotations"],
                    tbl_fqn,
                    schema_checker=schema_validation.check_relationships_annotations,
                    content_checker=relationship_annotations.check_entry,
                ),
            )

        if "ddl" in artifacts:
            ddl_path = tbl_dir / artifacts["ddl"]

            if ddl_path.is_file():
                issues.extend(ddl.check(ddl_path, _rel(print_root, ddl_path)))

    issues.extend(relationships.check_reciprocity(print_root, manifest_data, on_table=sinks[2]))
    issues.extend(
        relationships.check_observed_arithmetic(print_root, manifest_data, on_table=sinks[3]),
    )
    issues.extend(column_annotations.check_stale_keys(print_root, manifest_data, on_table=sinks[4]))
    issues.extend(column_annotations.check_claims(print_root, manifest_data, on_table=sinks[5]))
    issues.extend(
        column_annotations.check_value_notes(print_root, manifest_data, on_table=sinks[6]),
    )
    issues.extend(
        column_annotations.check_grain_annotations(print_root, manifest_data, on_table=sinks[7]),
    )
    issues.extend(
        relationship_annotations.check_verdicts(print_root, manifest_data, on_table=sinks[8]),
    )

    # Findings are known only once every pass above has run, so this tick fires after the fact.
    edge_claims_issues = relationship_annotations.check_claims(print_root, manifest_data)
    issues.extend(edge_claims_issues)

    if on_table is not None:
        findings = _findings_by_table(issues, tables)

        for i, tbl_fqn in enumerate(tables, start=1):
            on_table(
                ValidationTick(
                    fqn=tbl_fqn,
                    index=i,
                    total=total,
                    pass_name=VALIDATION_PASSES[-1],
                    pass_index=pass_total,
                    pass_total=pass_total,
                    findings=findings.get(tbl_fqn, 0),
                ),
            )

    manifest_annotations_path = print_root / "manifest.annotations.yaml"

    if manifest_annotations_path.is_file():
        rel = "manifest.annotations.yaml"

        try:
            manifest_annotations_data = load_yaml(manifest_annotations_path)
        except yaml.YAMLError as exc:
            issues.append(Issue(rel, "schema.invalid-yaml", "error", str(exc), "§2.7.3"))
        else:
            issues.extend(format_version.check(manifest_annotations_data, rel))
            issues.extend(
                schema_validation.check_manifest_annotations(manifest_annotations_data, rel),
            )

    diff_path = print_root / "diff.yaml"

    if diff_path.is_file():
        rel = "diff.yaml"

        try:
            diff_data = load_yaml(diff_path)
        except yaml.YAMLError as exc:
            issues.append(Issue(rel, "schema.invalid-yaml", "error", str(exc), "§2.6"))
        else:
            issues.extend(format_version.check(diff_data, rel))
            issues.extend(schema_validation.check_diff(diff_data, rel))
            issues.extend(diff.check(diff_data, rel))
            issues.extend(manifest.check_selectors_agree_with_diff(manifest_data, diff_data, rel))

    return sorted(issues)


def _check_artifact(
    print_root: Path,
    tbl_dir: Path,
    filename: str,
    tbl_fqn: str,
    schema_checker,
    content_checker,
) -> list[Issue]:
    artifact_path = tbl_dir / filename
    rel = _rel(print_root, artifact_path)

    if not artifact_path.is_file():
        return []  # caught by manifest.missing-artifact

    issues: list[Issue] = []

    try:
        data = load_yaml(artifact_path)
    except yaml.YAMLError as exc:
        return [Issue(rel, "schema.invalid-yaml", "error", str(exc), "§2")]

    issues.extend(format_version.check(data, rel))
    issues.extend(schema_checker(data, rel))
    issues.extend(content_checker(data, rel, tbl_fqn))

    return issues


def _rel(print_root: Path, p: Path) -> str:
    return str(p.relative_to(print_root))


def _pass_sink(
    on_table: ValidationProgress | None,
    pass_name: str,
    pass_index: int,
    pass_total: int,
) -> TableSink | None:
    """Wrap the caller's `ValidationTick`-shaped `on_table` into one pass's own `TableSink`."""

    if on_table is None:
        return None

    def sink(fqn: str, index: int, total: int) -> None:
        on_table(ValidationTick(fqn, index, total, pass_name, pass_index, pass_total))

    return sink


def _findings_by_table(issues: list[Issue], tables: dict[str, dict]) -> dict[str, int]:
    """Count issues per table, attributed by directory prefix of `Issue.path`.

    An issue with no table directory prefix (a connection-level file) attributes to none.
    """

    dir_to_fqn = {entry.get("path", ""): fqn for fqn, entry in tables.items() if entry.get("path")}
    counts: dict[str, int] = {}

    for issue in issues:
        parts = issue.path.split("/")

        for n in range(len(parts) - 1, 0, -1):
            fqn = dir_to_fqn.get("/".join(parts[:n]))

            if fqn is not None:
                counts[fqn] = counts.get(fqn, 0) + 1
                break

    return counts
