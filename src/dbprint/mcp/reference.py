"""Packaged SPEC.md/ASSERTIONS.md, sliced by heading (MCP.md 4.6).

Read through `importlib.resources` against the installed package first, the only path a wheel
install has. `hatch_build.py` force-includes both documents at build time rather than committing
a second copy, so an editable install falls back to the source the build hook itself reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from . import errors


ReferenceDocument = Literal["spec", "assertions"]

_PACKAGE_FILE: dict[ReferenceDocument, tuple[str, str]] = {
    "spec": ("dbprint.spec.v1", "SPEC.md"),
    "assertions": ("dbprint.assertions", "ASSERTIONS.md"),
}

# Mirrors `hatch_build.PACKAGED`'s source side - an editable install resolves inside the
# checked-out repo, where the build hook's own input is on disk. Tested to agree with it.
_SOURCE_TREE_FALLBACK: dict[ReferenceDocument, str] = {
    "spec": "docs/format/v1/SPEC.md",
    "assertions": "docs/ASSERTIONS.md",
}

# src/dbprint/mcp/reference.py -> repo root (parents[0]=mcp, [1]=dbprint, [2]=src, [3]=root).
_REPO_ROOT = Path(__file__).resolve().parents[3]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(\S.*)$")
_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?(?:\s|$)")
_FENCE_RE = re.compile(r"^```")

# Strips a verbatim `spec_ref` citation - optional document name, then the section sign - so a
# citation copied out of `dbprint check --format json` is usable as `section` unedited.
_CITATION_PREFIX_RE = re.compile(r"^(?:\S+\s+)?§\s*")


@dataclass(frozen=True)
class _Heading:
    """One parsed heading: its level, section number (if any), title, and line range."""

    level: int
    number: str | None
    title: str
    start: int
    end: int


def read_document(document: ReferenceDocument) -> str:
    """The whole document verbatim - the browsable resource form."""

    return _read(document)


def heading_tree(document: ReferenceDocument) -> str:
    """A markdown list of every heading (numbered or not), indented by nesting level.

    What a caller gets back for "no section given".
    """

    return heading_tree_of(_read(document))


def section(document: ReferenceDocument, number: str) -> str | None:
    """The heading numbered `number`, plus everything up to the next heading at or above its level.

    `number` is bare (`"6.1"`) or a verbatim `spec_ref` citation; `None` when no heading has it.
    """

    return section_of(_read(document), number)


def heading_tree_of(text: str) -> str:
    """`heading_tree()`'s own logic, over already-loaded text - independently testable."""

    headings = _parse_headings(text)
    base = min((h.level for h in headings), default=2)
    lines = [f"{'  ' * (h.level - base)}- {h.title}" for h in headings]

    return "\n".join(lines) + "\n"


def section_numbers(document: ReferenceDocument) -> list[str]:
    """Every heading's own number, in document order - named by an unknown-section error."""

    return [h.number for h in _parse_headings(_read(document)) if h.number is not None]


def section_of(text: str, number: str) -> str | None:
    """`section()`'s own logic, over already-loaded text - independently testable."""

    cleaned = _CITATION_PREFIX_RE.sub("", number).strip()
    lines = text.splitlines()

    for h in _parse_headings(text):
        if h.number == cleaned:
            return "\n".join(lines[h.start : h.end]).rstrip() + "\n"

    return None


def _read(document: ReferenceDocument) -> str:
    package, filename = _PACKAGE_FILE[document]

    try:
        return resources.files(package).joinpath(filename).read_text(encoding="utf-8")
    except FileNotFoundError:
        pass

    fallback = _REPO_ROOT / _SOURCE_TREE_FALLBACK[document]

    if fallback.is_file():
        return fallback.read_text(encoding="utf-8")

    raise errors.no_reference_document_available(document)


def _parse_headings(text: str) -> list[_Heading]:
    """Every heading outside a fenced code block, with each one's subtree line range.

    A fence-blind scan would misread a `#` line inside an example block as a heading.
    """

    lines = text.splitlines()
    raw: list[tuple[int, int, str]] = []
    in_fence = False

    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence

            continue

        if in_fence:
            continue

        m = _HEADING_RE.match(line)

        if m:
            raw.append((len(m.group(1)), i, m.group(2).strip()))

    headings: list[_Heading] = []

    for idx, (level, start, title) in enumerate(raw):
        end = len(lines)

        for next_level, next_start, _ in raw[idx + 1 :]:
            if next_level <= level:
                end = next_start

                break

        number_match = _NUMBER_RE.match(title)
        number = number_match.group(1) if number_match else None
        headings.append(_Heading(level=level, number=number, title=title, start=start, end=end))

    return headings
