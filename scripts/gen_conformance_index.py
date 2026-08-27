"""Render SPEC 6.3's error catalog into docs/reference/conformance.md, one flat list by code.

The specification groups codes by concern; this sorts by code so a pasted one can be found.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "docs" / "format" / "v1" / "SPEC.md"
DOCS_PATH = ROOT / "docs" / "reference" / "conformance.md"

CATALOG_START = "### 6.3 Error catalog"
CATALOG_END = "### 6.4 Catalog totals"

SEVERITIES = {"E": "error", "W": "warning"}

# github-slugger, which Astro's pipeline and GitHub both use: lower-case, drop punctuation and
# symbols, then map each remaining space to a hyphen. `\w` keeps digits, letters and underscore.
_DROPPED = re.compile(r"[^\w\- ]", re.UNICODE)

_HEADER = """\
# Conformance codes

Every code conformance validation can raise - the full set `validate_print()` returns, and the set
`dbprint check` reports under conformance - sorted by code so one pasted from a failure can be
found. Data-quality assertions carry their own `assertion.*` codes, which are specified in
[ASSERTIONS.md](../ASSERTIONS.md) rather than here. This file is generated from the specification's
own catalog - do not edit it by hand. Run `just docs` to regenerate it.

A print conforms when no `error` is raised against it. A `warning` records an anomaly that does not
gate conformance; [SPEC 6.1](../format/v1/SPEC.md#61-severity-model) defines both, and
[SPEC 6.2](../format/v1/SPEC.md#62-issue-document-shape) defines the issue each one is reported in.
"""


def build_document() -> str:
    """Return the full text of the generated conformance index."""

    entries = parse_catalog(SPEC_PATH.read_text())
    errors = sum(1 for entry in entries if entry["severity"] == "error")
    warnings = len(entries) - errors
    rows = "\n".join(
        f"| `{entry['code']}` | {entry['severity']} "
        f"| [{entry['group']}](../format/v1/SPEC.md#{entry['anchor']}) | {entry['trigger']} |"
        for entry in sorted(entries, key=lambda entry: entry["code"])
    )

    return (
        f"{_HEADER}\n"
        f"{len(entries)} codes: {errors} error, {warnings} warning.\n\n"
        "| Code | Severity | Specified in | Trigger |\n|---|---|---|---|\n"
        f"{rows}\n"
    )


def parse_catalog(spec_text: str) -> list[dict[str, str]]:
    """Every catalog entry from SPEC 6.3, carrying the group heading each was listed under."""

    block = _section(spec_text, CATALOG_START, CATALOG_END)
    entries: list[dict[str, str]] = []
    group = ""

    for line in block.splitlines():
        if line.startswith("#### "):
            group = line.removeprefix("#### ").strip()
            continue

        if not line.startswith("|") or line.startswith("|--"):
            continue

        cells = [cell.strip() for cell in line.strip("|").split("|")]

        if len(cells) != 3 or not cells[0].startswith("`"):
            continue  # the `| Code | Sev | Trigger |` header, repeated once per group

        severity = SEVERITIES.get(cells[1])

        if severity is None:
            raise ValueError(f"SPEC 6.3 row {cells[0]} carries an unknown severity {cells[1]!r}")

        entries.append(
            {
                "code": cells[0].strip("`"),
                "severity": severity,
                "group": group,
                "anchor": slug(group),
                "trigger": cells[2],
            },
        )

    if not entries:
        raise ValueError(f"no catalog rows found between {CATALOG_START!r} and {CATALOG_END!r}")

    return entries


def slug(heading: str) -> str:
    """The heading anchor Astro and GitHub both derive from a heading's text."""

    return _DROPPED.sub("", heading.lower()).replace(" ", "-")


def write_document() -> None:
    """Render the index and write it to DOCS_PATH, creating parent dirs."""

    DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOCS_PATH.write_text(build_document())


def _section(text: str, start: str, end: str) -> str:
    """The text between two headings, each of which must occur exactly once."""

    for marker in (start, end):
        found = text.count(marker)

        if found != 1:
            raise ValueError(f"{marker!r} occurs {found} times in SPEC.md, expected exactly once")

    return text[text.index(start) : text.index(end)]


if __name__ == "__main__":
    write_document()
    print(f"wrote {DOCS_PATH}")
