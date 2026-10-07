"""Packaged SPEC.md/ASSERTIONS.md and the reading guide, sliced by heading (MCP.md 4.6).

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

from dbprint.engine.reading_guide import READING_GUIDE_TEXT
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
    """The heading numbered `number` and its own body, then a list of its direct subsections.

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
    headings = _parse_headings(text)

    for index, h in enumerate(headings):
        if h.number == cleaned:
            return _slice(text.splitlines(), headings, index, by_number=True)

    return None


def guide_heading_tree() -> str:
    """The packaged reading guide's heading tree - this dbprint version's guide, not a print's."""

    return heading_tree_of(READING_GUIDE_TEXT)


def guide_section(heading: str) -> str | None:
    """The guide heading whose title matches `heading`, case- and whitespace-insensitively."""

    return guide_section_of(READING_GUIDE_TEXT, heading)


def guide_headings() -> list[str]:
    """Every guide heading's title, in document order - named by an unknown-heading error."""

    return [h.title for h in _parse_headings(READING_GUIDE_TEXT)]


def guide_section_of(text: str, heading: str) -> str | None:
    """`guide_section()`'s own logic, over already-loaded text - independently testable."""

    wanted = _folded(heading)
    headings = _parse_headings(text)

    for index, h in enumerate(headings):
        if _folded(h.title) == wanted:
            return _slice(text.splitlines(), headings, index, by_number=False)

    return None


def _slice(lines: list[str], headings: list[_Heading], index: int, *, by_number: bool) -> str:
    """A heading's own body up to its first addressable subsection, then those subsections listed.

    Under `by_number` an unnumbered heading cannot be asked for, so its text stays in the parent's.
    """

    h = headings[index]
    inside = [c for c in headings[index + 1 :] if c.start < h.end]
    direct = [c for c in inside if not any(o.start < c.start < o.end for o in inside)]
    children = [c for c in direct if c.number is not None] if by_number else direct
    end = children[0].start if children else h.end
    body = "\n".join(lines[h.start : end]).rstrip() + "\n"

    if not children:
        return body

    listed = "\n".join(f"- {c.title}" for c in children)
    addressed = "number" if by_number else "heading"

    return f"{body}\nSubsections, each read by its own {addressed}:\n\n{listed}\n"


def _folded(title: str) -> str:
    return " ".join(title.split()).casefold()


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
