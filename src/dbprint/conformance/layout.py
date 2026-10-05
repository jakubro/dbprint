"""Layout checks per SPEC 1."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import yaml

from .issue import Issue
from .progress import TableSink
from .yaml_utils import load_yaml


PATH_SEGMENT_RE = re.compile(r"^[a-z0-9_][a-z0-9_-]*$")
PRODUCER_ARTIFACTS = {"ddl.sql", "statistics.yaml", "relationships.yaml"}
_STALE_TABLE_REMEDY = (
    " A table an earlier release printed under this name is refused now: exclude it, delete this "
    "directory, and the next generate drops its entry."
)
CANONICAL_ARTIFACTS = {
    *PRODUCER_ARTIFACTS,
    "description.md",
    "statistics.annotations.yaml",
    "relationships.annotations.yaml",
}


def check(print_root: Path) -> tuple[list[Issue], dict | None]:
    """Run layout checks; also returns parsed manifest data when present."""

    issues: list[Issue] = []
    manifest_path = print_root / "manifest.yaml"

    if not manifest_path.is_file():
        issues.append(
            Issue(
                "manifest.yaml",
                "layout.missing-manifest",
                "error",
                "Connection root is missing manifest.yaml.",
                "§1.2",
            ),
        )

        return issues, None

    try:
        manifest_data = load_yaml(manifest_path)
    except yaml.YAMLError as exc:
        issues.append(Issue("manifest.yaml", "schema.invalid-yaml", "error", str(exc), "§2.5"))

        return issues, None

    # Reported as a finding, not raised, so a malformed manifest still returns a result.
    if not isinstance(manifest_data, dict):
        issues.append(
            Issue(
                "manifest.yaml",
                "schema.type-mismatch",
                "error",
                f"manifest.yaml must hold a mapping, found {type(manifest_data).__name__}.",
                "§2.5",
            ),
        )

        return issues, None

    tables = manifest_data.get("tables")

    if tables is not None and not isinstance(tables, dict):
        issues.append(
            Issue(
                "manifest.yaml::tables",
                "schema.type-mismatch",
                "error",
                f"`tables` must map each table name to its entry, found {type(tables).__name__}.",
                "§2.5",
            ),
        )

        return issues, None

    issues.extend(_check_path_segments(print_root))
    issues.extend(_check_per_table_files(print_root, manifest_data))
    issues.extend(_check_directory_depth(print_root, manifest_data))
    issues.extend(_check_reading_guide_present(print_root))
    issues.extend(_check_diff_present(print_root, manifest_data))

    return issues, manifest_data


def walkable_tables(manifest_data: dict) -> dict:
    """The manifest entries a check can read, keyed by table name.

    A non-mapping entry, or one whose `path` is not a string, drops silently here; the
    schema check already reports it.
    """

    tables = manifest_data.get("tables") or {}

    return {
        fqn: entry
        for fqn, entry in tables.items()
        if isinstance(entry, dict) and isinstance(entry.get("path", ""), str)
    }


def declared_artifacts(tbl_entry: dict) -> dict:
    """The artifacts map a check can read, empty when the entry has none readable."""

    artifacts = tbl_entry.get("artifacts") or {}

    if not isinstance(artifacts, dict):
        return {}

    return {kind: name for kind, name in artifacts.items() if isinstance(name, str)}


def annotated_tables(
    print_root: Path,
    manifest_data: dict,
    annotations: str,
    sibling: str,
    on_table: TableSink | None = None,
) -> Iterator[tuple[Path, dict, dict]]:
    """Each table's annotations path, its parsed body and its sibling artifact's parsed body.

    Skips a table missing a file or with an unparseable body; `on_table` fires per walked table.
    """

    tables = walkable_tables(manifest_data)
    total = len(tables)

    for i, (tbl_fqn, tbl_entry) in enumerate(tables.items(), start=1):
        if on_table is not None:
            on_table(tbl_fqn, i, total)

        artifacts = declared_artifacts(tbl_entry)

        if annotations not in artifacts or sibling not in artifacts:
            continue

        tbl_dir = print_root / tbl_entry.get("path", "")
        ann_path = tbl_dir / artifacts[annotations]
        sibling_path = tbl_dir / artifacts[sibling]

        if not ann_path.is_file() or not sibling_path.is_file():
            continue

        try:
            ann_data = load_yaml(ann_path)
            sibling_data = load_yaml(sibling_path)
        except yaml.YAMLError:
            continue

        if isinstance(ann_data, dict) and isinstance(sibling_data, dict):
            yield ann_path, ann_data, sibling_data


def _check_path_segments(print_root: Path) -> list[Issue]:
    issues: list[Issue] = []

    for path in print_root.rglob("*"):
        if path.is_dir() and not PATH_SEGMENT_RE.match(path.name):
            issues.append(
                Issue(
                    str(path.relative_to(print_root)),
                    "layout.invalid-path-segment",
                    "error",
                    f"Path segment {path.name!r} fails the allowlist regex {PATH_SEGMENT_RE.pattern!r}."
                    + _STALE_TABLE_REMEDY,
                    "§1.5.1",
                ),
            )

    return issues


def _check_per_table_files(print_root: Path, manifest_data: dict) -> list[Issue]:
    issues: list[Issue] = []

    for tbl_entry in walkable_tables(manifest_data).values():
        tbl_path = tbl_entry.get("path", "")
        tbl_dir = print_root / tbl_path

        if not tbl_dir.is_dir():
            continue

        for child in tbl_dir.iterdir():
            if child.is_file() and child.name not in CANONICAL_ARTIFACTS:
                issues.append(
                    Issue(
                        str(child.relative_to(print_root)),
                        "layout.unknown-file",
                        "warning",
                        f"File {child.name!r} is not in the canonical artifact list.",
                        "§1.4",
                    ),
                )

    return issues


def _check_reading_guide_present(print_root: Path) -> list[Issue]:
    """SPEC 1.2: `reading.md` is REQUIRED at every connection root."""

    if (print_root / "reading.md").is_file():
        return []

    return [
        Issue(
            "reading.md",
            "layout.missing-reading-guide",
            "error",
            "Connection root is missing reading.md.",
            "§1.2",
        ),
    ]


def _check_diff_present(print_root: Path, manifest_data: dict) -> list[Issue]:
    """SPEC 1.2: `diff.yaml` is REQUIRED once the manifest records any table.

    A validator holding one directory cannot know whether a generate "succeeded"; the manifest
    naming a table is the decidable proxy, so an empty manifest is exempt.
    """

    if not walkable_tables(manifest_data) or (print_root / "diff.yaml").is_file():
        return []

    return [
        Issue(
            "diff.yaml",
            "layout.missing-diff",
            "error",
            "Connection root is missing diff.yaml, though the manifest records a table.",
            "§1.2",
        ),
    ]


def _check_directory_depth(print_root: Path, manifest_data: dict) -> list[Issue]:
    """Producer-written artifacts must sit in a directory some manifest table path declares.

    A user-authored file outside one is `manifest.orphaned-artifact`'s alone (SPEC 1.4).
    """

    issues: list[Issue] = []
    valid_table_dirs = {
        tuple((print_root / e.get("path", "")).resolve().parts)
        for e in walkable_tables(manifest_data).values()
    }

    for path in print_root.rglob("*"):
        if not path.is_file() or path.name not in PRODUCER_ARTIFACTS:
            continue

        parent_parts = path.parent.resolve().parts

        if parent_parts in valid_table_dirs:
            continue

        issues.append(
            Issue(
                str(path.relative_to(print_root)),
                "layout.unexpected-directory-level",
                "error",
                f"Producer-written artifact {path.name!r} appears in a directory not listed in manifest.tables[*].path.",
                "§1.4",
            ),
        )

    return issues
