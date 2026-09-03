"""Shared SPEC.md markdown-table parser for tests checking prose against code.

Reads the markdown itself, so a guard built here checks the specification, not a mirror.
"""

from __future__ import annotations

import re
from pathlib import Path


SPEC = Path(__file__).resolve().parents[2] / "docs/format/v1/SPEC.md"

_BACKTICKED = re.compile(r"`([^`]+)`")


def section(start: str, end: str) -> str:
    """The text between two headings, each of which must occur exactly once in SPEC.md."""

    return section_of(SPEC, start, end)


def section_of(path: Path, start: str, end: str) -> str:
    """The text between two headings, each of which must occur exactly once in `path`."""

    text = path.read_text(encoding="utf-8")

    for marker in (start, end):
        found = text.count(marker)
        assert found == 1, f"{marker!r} occurs {found} times in {path.name}, expected exactly once"

    return text[text.index(start) : text.index(end)]


def table_rows(block: str) -> list[list[str]]:
    """Every markdown table row in `block`, as stripped cells, separators dropped."""

    rows = [
        line for line in block.splitlines() if line.startswith("|") and not line.startswith("|--")
    ]

    return [[cell.strip() for cell in line.strip("|").split("|")] for line in rows]


def matrix() -> dict[str, list[str]]:
    """SPEC 2.2.3's field matrix as {field name: verdict per classification}."""

    rows = table_rows(section("#### 2.2.3", "#### 2.2.4"))
    header, body = rows[0], rows[1:]
    classifications = header[1:]
    out: dict[str, list[str]] = {}

    for cells in body:
        name = _BACKTICKED.search(cells[0])
        assert name is not None, f"matrix row names no field: {cells[0]!r}"
        assert len(cells) - 1 == len(classifications), f"ragged matrix row: {cells[0]!r}"
        out[name.group(1)] = cells[1:]

    return out


def matrix_classifications() -> list[str]:
    """The bare classification names (backticks stripped), in the matrix's own column order."""

    header = table_rows(section("#### 2.2.3", "#### 2.2.4"))[0]
    names: list[str] = []

    for cell in header[1:]:
        match = _BACKTICKED.search(cell)
        assert match is not None, f"matrix header names no classification: {cell!r}"
        names.append(match.group(1))

    return names
