"""SPEC.md's tables for tests checking prose against code, through the generators' own parser.

Reads the markdown itself, so a guard built here checks the specification, not a mirror.
"""

from __future__ import annotations

from pathlib import Path

from tests._scripts import load_script


_PARSER = load_script("spec_markdown")

SPEC = _PARSER.SPEC_PATH
table_rows = _PARSER.table_rows


def section(start: str, end: str) -> str:
    """The text between two headings, each of which must occur exactly once in SPEC.md."""

    return section_of(SPEC, start, end)


def section_of(path: Path, start: str, end: str) -> str:
    """The text between two headings, each of which must occur exactly once in `path`."""

    return _PARSER.section(path.read_text(encoding="utf-8"), start, end)


def matrix() -> dict[str, list[str]]:
    """SPEC 2.2.3's field matrix as {field name: verdict per classification}."""

    return _PARSER.matrix(SPEC.read_text(encoding="utf-8"))


def matrix_classifications() -> list[str]:
    """The bare classification names (backticks stripped), in the matrix's own column order."""

    return _PARSER.matrix_classifications(SPEC.read_text(encoding="utf-8"))


def error_catalog() -> list[dict[str, str]]:
    """Every SPEC 6.3 row as `code`, single-letter `severity`, `trigger` and `group`."""

    return _PARSER.error_catalog(SPEC.read_text(encoding="utf-8"))
