"""Follow an MCP tool's `next_cursor` to its last page and reassemble the pages (MCP.md 4.8)."""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import yaml

from dbprint.mcp import ServedConnections, dispatch


_MARKER_RE = re.compile(r"\n(?:<!-- next_cursor: (\S+) -->|# next_cursor: (\S+)\n)\Z")


def pages(state: ServedConnections, tool: str, arguments: dict[str, Any]) -> list[Any]:
    """Every page of one call, in order."""

    out = [dispatch(state, tool, arguments)]

    while (cursor := next_cursor(out[-1])) is not None:
        out.append(dispatch(state, tool, {**arguments, "cursor": cursor}))

    return out


def next_cursor(page: Any) -> str | None:
    """A dict page's `next_cursor` key, or a text page's trailing cursor line."""

    if isinstance(page, dict):
        return page.get("next_cursor")

    match = _MARKER_RE.search(page)

    return None if match is None else match.group(1) or match.group(2)


_LEGEND = r"## Terms\n\n(?:- [^\n]*\n)*- [^\n]*"
_FIRST_PAGE_LEGEND_RE = re.compile(rf"\n\n{_LEGEND}(?=\n|\Z)")
_LATER_PAGE_LEGEND_RE = re.compile(rf"\A{_LEGEND}\n\n")


def joined(text_pages: list[str]) -> str:
    """Text pages with their cursor lines and each page's legend removed, concatenated."""

    return "".join(
        without_legend(_MARKER_RE.sub("", page), first=number == 0)
        for number, page in enumerate(text_pages)
    )


def without_legend(text: str, *, first: bool = True) -> str:
    """`text` less its `## Terms` legend: after the header on a first page, else at the top."""

    if first:
        return _FIRST_PAGE_LEGEND_RE.sub("", text, count=1)

    return _LATER_PAGE_LEGEND_RE.sub("", text, count=1)


def merged(object_pages: list[Any]) -> dict[str, Any]:
    """Pages merged key by key, lists concatenated, and an item sent in parts rebuilt."""

    out: dict[str, Any] = {}

    for page in object_pages:
        data = yaml.safe_load(page) if isinstance(page, str) else copy.deepcopy(page)
        _merge(out, {k: v for k, v in data.items() if k != "next_cursor"})

    return _rebuilt(out)


def _merge(into: dict[str, Any], page: dict[str, Any]) -> None:
    for key, value in page.items():
        if _is_part(value):
            into.setdefault(key, []).append(value)
        elif isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        elif isinstance(value, list) and isinstance(into.get(key), list):
            into[key] = into[key] + value
        else:
            into[key] = value


def _rebuilt(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _rebuilt(item) for key, item in value.items()}

    if not isinstance(value, list):
        return value

    out: list[Any] = []
    run: list[dict[str, Any]] = []

    for item in value:
        if not _is_part(item):
            out.append(_rebuilt(item))
            continue

        run.append(item)

        if item["part"] == item["parts"]:
            out.append(json.loads("".join(part["text"] for part in run)))
            run = []

    return out[0] if out and all(_is_part(v) for v in value) else out


def _is_part(value: Any) -> bool:
    return isinstance(value, dict) and set(value) == {"part", "parts", "text"}
