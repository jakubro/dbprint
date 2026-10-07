"""SPEC.md's headings and tables, read by the doc generators and by the tests checking prose.

Loaded by path, since `scripts/` is not a package.
"""

from __future__ import annotations

import re
from pathlib import Path


SPEC_PATH = Path(__file__).resolve().parents[1] / "docs/format/v1/SPEC.md"

MATRIX_START = "#### 2.2.3"
MATRIX_END = "#### 2.2.4"
CATALOG_START = "### 6.3 Error catalog"
CATALOG_END = "### 6.4 Catalog totals"

_BACKTICKED = re.compile(r"`([^`]+)`")


def section(text: str, start: str, end: str) -> str:
    """The text between two headings, each of which must occur exactly once in `text`."""

    for marker in (start, end):
        found = text.count(marker)

        if found != 1:
            raise ValueError(f"{marker!r} occurs {found} times, expected exactly once")

    return text[text.index(start) : text.index(end)]


def table_rows(block: str) -> list[list[str]]:
    """Every markdown table row in `block`, as stripped cells, separators dropped."""

    rows = [
        line for line in block.splitlines() if line.startswith("|") and not line.startswith("|--")
    ]

    return [[cell.strip() for cell in line.strip("|").split("|")] for line in rows]


def matrix_classifications(spec: str) -> list[str]:
    """SPEC 2.2.3's classification names (backticks stripped), in the matrix's column order."""

    header = table_rows(section(spec, MATRIX_START, MATRIX_END))[0]
    names: list[str] = []

    for cell in header[1:]:
        match = _BACKTICKED.search(cell)

        if match is None:
            raise ValueError(f"matrix header names no classification: {cell!r}")

        names.append(match.group(1))

    return names


def matrix(spec: str) -> dict[str, list[str]]:
    """SPEC 2.2.3's field matrix as {field name: verdict per classification}."""

    width = len(matrix_classifications(spec))
    out: dict[str, list[str]] = {}

    for cells in table_rows(section(spec, MATRIX_START, MATRIX_END))[1:]:
        name = _BACKTICKED.search(cells[0])

        if name is None:
            raise ValueError(f"matrix row names no field: {cells[0]!r}")

        if len(cells) - 1 != width:
            raise ValueError(f"ragged matrix row: {cells[0]!r}")

        out[name.group(1)] = cells[1:]

    return out


def error_catalog(spec: str) -> list[dict[str, str]]:
    """Every SPEC 6.3 row as `code`, single-letter `severity`, `trigger` and its `group` heading."""

    entries: list[dict[str, str]] = []
    group = ""

    for line in section(spec, CATALOG_START, CATALOG_END).splitlines():
        if line.startswith("#### "):
            group = line.removeprefix("#### ").strip()
            continue

        cells = table_rows(line)

        # The `| Code | Sev | Trigger |` header repeats once per group.
        if not cells or len(cells[0]) != 3 or not cells[0][0].startswith("`"):
            continue

        code, severity, trigger = cells[0]
        entries.append(
            {"code": code.strip("`"), "severity": severity, "group": group, "trigger": trigger},
        )

    if not entries:
        raise ValueError(f"no catalog rows found between {CATALOG_START!r} and {CATALOG_END!r}")

    return entries
