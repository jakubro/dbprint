"""Cursor paging for every MCP reply (MCP.md 4.8): pages under a character bound, cursors
pinned to the files the reply read, and an item too large for a page sent in parts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import errors


PAGE_CHARACTERS = 20_000

# Fixed width, so a page measured with a placeholder cursor measures the same as with the real one.
_POSITION_DIGITS = 10
_DIGEST_CHARS = 16


@dataclass(frozen=True)
class Part:
    """One slice of an item too large for a page; the slices' `text` concatenated is its JSON."""

    key: Any
    part: int
    parts: int
    text: str

    def payload(self) -> dict[str, Any]:
        """The shape a part takes in a reply, in the item's own place."""

        return {"part": self.part, "parts": self.parts, "text": self.text}


@dataclass(frozen=True)
class Call:
    """What a cursor continues: the tool, its arguments bar `cursor`, the files the reply read."""

    tool: str
    arguments: dict[str, Any]
    files: tuple[Path, ...]

    def cursor(self, position: int) -> str:
        """The opaque cursor resuming this call at `position`."""

        token = json.dumps(
            {
                "t": self.tool,
                "p": f"{position:0{_POSITION_DIGITS}d}",
                "a": self._arguments_digest(),
                "f": files_digest(self.files),
            },
            separators=(",", ":"),
        )

        return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii").rstrip("=")

    def position(self, cursor: str | None) -> int:
        """Where a call resumes; raises McpError for a cursor this call did not issue."""

        if cursor is None:
            return 0

        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            token = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            tool, position, arguments, files = token["t"], token["p"], token["a"], token["f"]
            index = int(position)
        except (binascii.Error, UnicodeError, ValueError, TypeError, KeyError) as exc:
            raise errors.malformed_cursor(cursor) from exc

        if tool != self.tool or arguments != self._arguments_digest():
            raise errors.foreign_cursor(self.tool)

        if files != files_digest(self.files):
            raise errors.stale_cursor(self.tool)

        return index

    def _arguments_digest(self) -> str:
        canonical = json.dumps(
            {k: v for k, v in self.arguments.items() if k != "cursor"},
            sort_keys=True,
            default=str,
        )

        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def files_digest(paths: Iterable[Path]) -> str:
    """Digest of each file's identity on disk (MCP.md 7.1); an absent file counts as absent."""

    entries = []

    for path in sorted(set(paths)):
        try:
            stat = os.stat(path)
            entries.append(f"{path}:{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}")
        except OSError:
            entries.append(f"{path}:absent")

    return hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()[:_DIGEST_CHARS]


def serialized(reply: Any) -> str:
    """A reply as the client receives it: the server's own `json.dumps` of a dict, a string as is."""

    return reply if isinstance(reply, str) else json.dumps(reply, default=str, indent=2)


Render = Callable[[Sequence[Any], bool, str | None], Any]


def page(
    call: Call,
    units: Sequence[Any],
    render: Render,
    cursor: str | None,
) -> Any:
    """The reply for the page `cursor` points at; no cursor is the first page.

    `render(units, first, next_cursor)` must grow with the run and carry any header on page one.
    """

    first = cursor is None
    start = call.position(cursor)

    if start > len(units) or (not first and start == len(units)):
        raise errors.foreign_cursor(call.tool)

    count = _longest_run(units, start, first, render, call.cursor(0))

    if count == len(units) - start:
        return render(units[start:], first, None)

    if count == 0 and not first:
        count = 1

    return render(units[start : start + count], first, call.cursor(start + count))


def split_oversize(
    items: Sequence[tuple[Any, Any]],
    render: Render,
    placeholder: str,
) -> list[Any]:
    """`(key, value)` items as units, each value too large for a page replaced by its `Part`s.

    A part's text is sized so the part alone, on a page carrying a cursor, stays in the bound.
    """

    units: list[Any] = []

    for key, value in items:
        if _fits(render([(key, value)], False, placeholder)):
            units.append((key, value))
            continue

        overhead = len(serialized(render([Part(key, 9999, 9999, "")], False, placeholder)))
        chunks = _chunks(json.dumps(value, default=str), PAGE_CHARACTERS - overhead)
        units.extend(
            Part(key=key, part=n, parts=len(chunks), text=chunk)
            for n, chunk in enumerate(chunks, start=1)
        )

    return units


def text_page(
    call: Call,
    blocks: Sequence[str],
    cursor: str | None,
    marker: Callable[[str], str],
) -> str:
    """One page of `blocks` joined by blank lines; every page but the last ends in `marker(cursor)`.

    Pages break between blocks where one fits, else at line boundaries, else mid-line.
    """

    pages = _document_pages(blocks, PAGE_CHARACTERS - len(marker(call.cursor(0))))
    index = call.position(cursor)

    if index >= len(pages) or (cursor is not None and index == 0):
        raise errors.foreign_cursor(call.tool)

    if index == len(pages) - 1:
        return pages[index]

    return pages[index] + marker(call.cursor(index + 1))


def legend_text_page(
    call: Call,
    blocks: Sequence[tuple[str, Sequence[frozenset[str]]]],
    cursor: str | None,
    marker: Callable[[str], str],
    legend: Callable[[frozenset[str]], str],
) -> str:
    """`text_page` with a legend per page: the terms of that page's lines.

    After block one on page one, leading every later page; pages leave room for the whole legend.
    """

    texts = [text for text, _ in blocks]
    full = legend(frozenset().union(*(terms for _, line_terms in blocks for terms in line_terms)))
    reserve = len(full) + 2 if full else 0
    pages = _document_pages(texts, PAGE_CHARACTERS - len(marker(call.cursor(0))) - reserve)
    index = call.position(cursor)

    if index >= len(pages) or (cursor is not None and index == 0):
        raise errors.foreign_cursor(call.tool)

    start = sum(len(page) for page in pages[:index])
    terms = _terms_between(blocks, start, start + len(pages[index]))
    page_legend = legend(terms)
    text = pages[index]

    if page_legend and index == 0:
        head = min(len(texts[0]), len(text))
        text = f"{text[:head]}\n\n{page_legend}{text[head:]}"
    elif page_legend:
        text = f"{page_legend}\n\n{text}"

    if index == len(pages) - 1:
        return text

    return text + marker(call.cursor(index + 1))


def line_pages(
    text: str,
    bound: int = PAGE_CHARACTERS,
    width: Callable[[str], int] = len,
) -> list[str]:
    """`text` cut at line boundaries, a line wider than `bound` mid-line; joins back to `text`.

    `width` measures a piece as the client will receive it, the raw length by default.
    """

    pages: list[str] = []
    current = ""
    current_width = 0

    for line in text.splitlines(keepends=True):
        while width(line) > bound:
            if current:
                pages.append(current)
                current, current_width = "", 0

            cut = _widest_prefix(line, bound, width)
            pages.append(line[:cut])
            line = line[cut:]

        line_width = width(line)

        if current and current_width + line_width > bound:
            pages.append(current)
            current, current_width = "", 0

        current += line
        current_width += line_width

    if current or not pages:
        pages.append(current)

    return pages


def entry(unit: Any) -> tuple[Any, Any]:
    """A unit as `(key, value)`, a `Part` standing in its item's place."""

    if isinstance(unit, Part):
        return unit.key, unit.payload()

    return unit


def escaped_width(text: str) -> int:
    """`text`'s length as a JSON string body, the form a resource read crosses the wire in."""

    return len(json.dumps(text, ensure_ascii=False)) - 2


def _widest_prefix(text: str, bound: int, width: Callable[[str], int]) -> int:
    total = 0

    for index, char in enumerate(text):
        total += width(char)

        if total > bound:
            return max(index, 1)

    return len(text)


def _fits(reply: Any) -> bool:
    return len(serialized(reply)) <= PAGE_CHARACTERS


def _longest_run(
    units: Sequence[Any],
    start: int,
    first: bool,
    render: Render,
    placeholder: str,
) -> int:
    def fits(count: int) -> bool:
        run = units[start : start + count]
        keys = [unit.key for unit in run if isinstance(unit, Part)]

        # A mapping holds one value per key, so two parts of one item never share a page.
        return len(keys) == len(set(keys)) and _fits(render(run, first, placeholder))

    remaining = len(units) - start
    low, high = 0, 1

    while high <= remaining and fits(high):
        low, high = high, high * 2

    high = min(high, remaining + 1)

    while high - low > 1:
        middle = (low + high) // 2

        if fits(middle):
            low = middle
        else:
            high = middle

    return low


def _document_pages(blocks: Sequence[str], bound: int) -> list[str]:
    pieces = [f"{block}\n\n" for block in blocks[:-1]] + list(blocks[-1:])
    pages: list[str] = []
    current = ""

    for piece in pieces:
        if len(current) + len(piece) <= bound:
            current += piece
            continue

        if current:
            pages.append(current)

        lines = line_pages(piece, bound)
        pages.extend(lines[:-1])
        current = lines[-1]

    if current or not pages:
        pages.append(current)

    return pages


def _terms_between(
    blocks: Sequence[tuple[str, Sequence[frozenset[str]]]],
    start: int,
    end: int,
) -> frozenset[str]:
    """The terms of every line the document's characters `start`..`end` touch."""

    found: set[str] = set()
    offset = 0

    for text, line_terms in blocks:
        for number, line in enumerate(text.split("\n")):
            if number < len(line_terms) and offset < end and offset + len(line) > start:
                found |= line_terms[number]

            offset += len(line) + 1

        offset += 1

    return frozenset(found)


def _chunks(text: str, budget: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0

    for char in text:
        width = len(json.dumps(char)) - 2

        if size + width > budget and current:
            chunks.append("".join(current))
            current, size = [], 0

        current.append(char)
        size += width

    chunks.append("".join(current))

    return chunks
