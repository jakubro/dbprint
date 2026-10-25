"""Admission of a human-authored annotation file: read only when it matches its schema (SPEC 2.7)."""

from __future__ import annotations

import importlib.resources
import json
from collections.abc import Iterator
from datetime import date, datetime
from functools import cache
from typing import Any

from jsonschema import Draft202012Validator


AUTHORED_KINDS = frozenset(
    {"statistics_annotations", "relationships_annotations", "manifest_annotations"},
)

Pointer = tuple[str | int, ...]


def schema(kind: str) -> dict[str, Any]:
    """The packaged JSON Schema of one annotation kind, loaded once."""

    return _schema(kind)


def non_string_keys(data: Any) -> list[Pointer]:
    """The pointer to every mapping key that is not a string, in document order."""

    return [pointer for pointer, key in _keys(data, ()) if not isinstance(key, str)]


def iso_dates(node: Any) -> Any:
    """`node` with every YAML date or timestamp value read as its ISO 8601 text, as a schema sees it."""

    if isinstance(node, datetime):
        return node.isoformat().replace("+00:00", "Z")
    elif isinstance(node, date):
        return node.isoformat()
    elif isinstance(node, dict):
        return {k: iso_dates(v) for k, v in node.items()}
    elif isinstance(node, list):
        return [iso_dates(v) for v in node]
    else:
        return node


def malformation(kind: str, data: Any) -> str | None:
    """None when `data` is admitted; else its first problem in document order, `<address>: <why>`."""

    if not isinstance(data, dict):
        return f"root: a {type(data).__name__}, not a mapping"

    data = iso_dates(data)
    # A key's problem sits at its mapping, ahead of the schema's own error there.
    problems = [
        (pointer[:-1], 0, f"{key_address(pointer)}: {key_problem(pointer[-1])}")
        for pointer in non_string_keys(data)
    ]

    for error in Draft202012Validator(schema(kind)).iter_errors(data):
        pointer = tuple(error.absolute_path)
        problems.append((pointer, 1, f"{_address(pointer) or 'root'}: {error.message}"))

    if not problems:
        return None

    order = {pointer: index for index, pointer in enumerate(_pointers(data, ()))}

    return min(problems, key=lambda problem: (_position(order, problem[0]), problem[1]))[2]


def admitted(kind: str, data: Any) -> dict[str, Any] | None:
    """`data` as a reader uses it, dates read as `iso_dates` does, when admitted; else None."""

    return iso_dates(data) if malformation(kind, data) is None else None


def key_address(pointer: Pointer) -> str:
    """A `non_string_keys` pointer spelled `.name`/`[i]`, its last segment always as a key."""

    return _address((*pointer[:-1], str(pointer[-1])))


def key_problem(key: Any) -> str:
    """Why a non-string mapping key is refused, naming the key and its type."""

    kind = next(t for t in type(key).__mro__ if t.__module__ == "builtins")

    return f"mapping key {key!r} ({kind.__name__}) is not a string; quote it"


@cache
def _schema(kind: str) -> dict[str, Any]:
    if kind not in AUTHORED_KINDS:
        raise ValueError(f"not an annotation kind: {kind!r}")

    files = importlib.resources.files("dbprint.spec.v1")

    return json.loads(files.joinpath(f"{kind}.schema.json").read_text(encoding="utf-8"))


def _keys(data: Any, prefix: Pointer) -> Iterator[tuple[Pointer, Any]]:
    if isinstance(data, dict):
        for key, value in data.items():
            yield (*prefix, key), key
            yield from _keys(value, (*prefix, key))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from _keys(value, (*prefix, index))


def _pointers(data: Any, prefix: Pointer) -> Iterator[Pointer]:
    yield prefix

    if isinstance(data, dict):
        for key, value in data.items():
            yield from _pointers(value, (*prefix, key))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            yield from _pointers(value, (*prefix, index))


def _position(order: dict[Pointer, int], pointer: Pointer) -> int:
    while pointer not in order:
        pointer = pointer[:-1]

    return order[pointer]


def _address(pointer: Pointer) -> str:
    rendered = ""

    for segment in pointer:
        if isinstance(segment, int) and not isinstance(segment, bool):
            rendered += f"[{segment}]"
        else:
            rendered += f".{segment}" if rendered else str(segment)

    return rendered
