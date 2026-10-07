"""Read back what a generate run wrote, through the same rules the product reads it by."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dbprint.conformance import Issue, validate_print
from dbprint.engine.baseline import table_directory
from dbprint.spec.artifacts import ARTIFACT_FILENAMES, MANIFEST_FILENAME


def conformance_errors(print_root: Path) -> list[Issue]:
    """The error-severity conformance issues of the print at `print_root`."""

    return [issue for issue in validate_print(print_root) if issue.severity == "error"]


def assert_conformant(print_root: Path) -> None:
    """Fail on any error-severity conformance issue in the print, listing each."""

    errors = conformance_errors(print_root)
    assert errors == [], "Conformance violations:\n" + "\n".join(
        f"  {e.code} at {e.path}: {e.detail}" for e in errors
    )


def manifest(print_root: Path) -> dict[str, Any]:
    """The print's parsed manifest."""

    return yaml.safe_load((print_root / MANIFEST_FILENAME).read_text())


def artifact(print_root: Path, fqn: str, kind: str = "statistics") -> dict[str, Any]:
    """One table's parsed artifact, found where the manifest entry says its directory is."""

    entry = manifest(print_root)["tables"][fqn]

    return yaml.safe_load(
        (table_directory(print_root, fqn, entry) / ARTIFACT_FILENAMES[kind]).read_text(),
    )
