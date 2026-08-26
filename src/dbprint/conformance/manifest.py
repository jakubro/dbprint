"""Manifest cross-checks per SPEC 2.5."""

from __future__ import annotations

from pathlib import Path

import yaml

from .issue import Issue
from .layout import CANONICAL_ARTIFACTS, declared_artifacts, walkable_tables
from .yaml_utils import load_yaml


_ARTIFACT_FILENAMES = {
    "ddl": "ddl.sql",
    "statistics": "statistics.yaml",
    "relationships": "relationships.yaml",
    "description": "description.md",
    "statistics_annotations": "statistics.annotations.yaml",
    "relationships_annotations": "relationships.annotations.yaml",
}


def check_manifest_annotations_presence(print_root: Path, manifest_data: dict) -> list[Issue]:
    """SPEC 2.7.3: `manifest_annotations` claims a connection-root file that must exist."""

    filename = manifest_data.get("manifest_annotations")

    if not isinstance(filename, str):
        return []

    if (print_root / filename).is_file():
        return []

    return [
        Issue(
            "manifest.yaml",
            "manifest.missing-artifact",
            "error",
            f"Manifest claims {filename} but file does not exist.",
            "§2.5",
        ),
    ]


def check(print_root: Path, manifest_data: dict) -> list[Issue]:
    """Cross-check manifest entries against the artifacts actually on disk."""

    issues: list[Issue] = []
    tables = walkable_tables(manifest_data)
    # Table dir -> the canonical filenames its entry declares; the second pass flags a file
    # on disk absent from this set as `manifest.orphaned-artifact`.
    declared_files: dict[Path, set[str]] = {}

    for tbl_fqn, tbl_entry in tables.items():
        tbl_path_str = tbl_entry.get("path", "")
        tbl_dir = print_root / tbl_path_str
        artifacts = declared_artifacts(tbl_entry)
        declared_files[tbl_dir.resolve()] = {
            _ARTIFACT_FILENAMES[key] for key in artifacts if key in _ARTIFACT_FILENAMES
        }

        for key, filename in _ARTIFACT_FILENAMES.items():
            if key not in artifacts:
                continue

            artifact_path = tbl_dir / filename
            rel = str(artifact_path.relative_to(print_root))

            if not artifact_path.is_file():
                issues.append(
                    Issue(
                        rel,
                        "manifest.missing-artifact",
                        "error",
                        f"Manifest claims {filename} for table {tbl_fqn} but file does not exist.",
                        "§2.5",
                    ),
                )
                continue

            if key in ("statistics", "relationships"):
                try:
                    data = load_yaml(artifact_path)
                except yaml.YAMLError:
                    continue

                if isinstance(data, dict):
                    declared_fqn = data.get("table")

                    if declared_fqn and declared_fqn != tbl_fqn:
                        issues.append(
                            Issue(
                                rel,
                                "manifest.table-fqn-mismatch",
                                "error",
                                f"Manifest FQN {tbl_fqn!r} does not match {filename} table field {declared_fqn!r}.",
                                "§2.5",
                            ),
                        )

                    if key == "statistics":
                        manifest_count = tbl_entry.get("columns")
                        stats_columns = data.get("columns")
                        # A scoped read that matched no rows carries an empty columns map by
                        # design (SPEC 2.6.4), while the manifest counts real DDL columns.
                        scoped_empty = not stats_columns and isinstance(data.get("scope"), dict)

                        if (
                            isinstance(manifest_count, int)
                            and isinstance(stats_columns, dict)
                            and not scoped_empty
                        ):
                            actual_count = len(stats_columns)

                            if manifest_count != actual_count:
                                issues.append(
                                    Issue(
                                        rel,
                                        "manifest.columns-count-mismatch",
                                        "error",
                                        f"Manifest declares columns: {manifest_count} for table "
                                        f"{tbl_fqn} but {filename}'s columns map carries "
                                        f"{actual_count}.",
                                        "§2.5",
                                    ),
                                )

    # Second pass: warn about a canonical file no manifest entry declares - an undeclared
    # directory, or a declared table whose artifact map omits this filename.
    root_resolved = print_root.resolve()

    for path in print_root.rglob("*"):
        if not path.is_file() or path.name not in CANONICAL_ARTIFACTS:
            continue

        parent_resolved = path.parent.resolve()

        if parent_resolved == root_resolved:
            continue

        if path.name not in declared_files.get(parent_resolved, set()):
            issues.append(
                Issue(
                    str(path.relative_to(print_root)),
                    "manifest.orphaned-artifact",
                    "warning",
                    f"File {path.name!r} on disk is not listed in any manifest entry.",
                    "§2.5",
                ),
            )

    return issues


def check_selectors_agree_with_diff(
    manifest_data: dict,
    diff_data: dict,
    path: str,
) -> list[Issue]:
    """SPEC 2.5/2.6: where both artifacts record selectors, the two copies MUST NOT disagree."""

    manifest_selectors = manifest_data.get("selectors")
    target = diff_data.get("target")
    diff_selectors = target.get("selectors") if isinstance(target, dict) else None

    if not isinstance(manifest_selectors, dict) or not isinstance(diff_selectors, dict):
        return []

    if manifest_selectors == diff_selectors:
        return []

    return [
        Issue(
            path,
            "manifest.selectors-mismatch-diff",
            "error",
            f"diff.yaml target.selectors={diff_selectors!r} disagrees with "
            f"manifest.yaml selectors={manifest_selectors!r}.",
            "§2.5",
        ),
    ]
