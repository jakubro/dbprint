"""Part paths per SPEC 2.2.18: parse, canonical spelling, parent, display, and which parts to keep.

A path is relative to its column: `[*]` an array's elements, `[keys]` a map's key set, `.name` or
`["any text"]` a member. The canonical spelling is unique, so two paths name one part exactly
when their strings are equal.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from .classification import is_floating_type


StepKind = Literal["member", "element", "keys"]

ELEMENT = "[*]"
KEYS = "[keys]"

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class Step:
    """One step of a part path; `name` is set on a member step only."""

    kind: StepKind
    name: str | None = None


def member(name: str) -> str:
    """The canonical step naming one member: `.name` when it is a plain name, else quoted."""

    if _NAME_RE.fullmatch(name):
        return f".{name}"

    return f"[{json.dumps(name, ensure_ascii=False)}]"


def parse(path: str) -> tuple[Step, ...]:
    """The steps of `path`; raises ValueError on a path the grammar does not admit."""

    steps: list[Step] = []
    position = 0

    while position < len(path):
        if path.startswith(ELEMENT, position):
            steps.append(Step("element"))
            position += len(ELEMENT)
        elif path.startswith(KEYS, position):
            steps.append(Step("keys"))
            position += len(KEYS)
        elif path.startswith(".", position):
            match = _NAME_RE.match(path, position + 1)

            if match is None:
                raise ValueError(f"part path {path!r}: no member name at offset {position}")

            steps.append(Step("member", match.group()))
            position = match.end()
        elif path.startswith('["', position):
            name, end = json.JSONDecoder().raw_decode(path, position + 1)

            if not isinstance(name, str) or not path.startswith("]", end):
                raise ValueError(f"part path {path!r}: unterminated member at offset {position}")

            steps.append(Step("member", name))
            position = end + 1
        else:
            raise ValueError(f"part path {path!r}: unexpected text at offset {position}")

    if not steps:
        raise ValueError("a part path holds at least one step")

    return tuple(steps)


def canonical(path: str) -> str:
    """`path` respelled canonically; raises ValueError where the grammar does not admit it."""

    return "".join(_spelled(step) for step in parse(path))


def is_canonical(path: str) -> bool:
    """Whether `path` parses and is already in its one canonical spelling."""

    try:
        return canonical(path) == path
    except ValueError:
        return False


def parent(path: str) -> str | None:
    """The part `path` sits inside - its longest proper prefix - or None for a top-level part."""

    steps = parse(path)

    return "".join(_spelled(step) for step in steps[:-1]) or None


def depth(path: str) -> int:
    """The number of steps in `path`."""

    return len(parse(path))


def last_member(path: str) -> str | None:
    """The name of the last member step in `path`, or None when it has none."""

    names = [step.name for step in parse(path) if step.kind == "member"]

    return names[-1] if names else None


def display(column: str, path: str) -> str:
    """How a reader shows a part: its column key followed by its path."""

    return f"{column}{path}"


def select_parts(
    candidates: Mapping[str, int],
    max_parts: int,
    max_part_depth: int,
) -> tuple[str, ...]:
    """The paths to profile, by `occurrences` descending then path text, each after its parent.

    `candidates` maps each found path to its occurrences; a path deeper than `max_part_depth`,
    or whose parent is never admitted, is never chosen.
    """

    ordered = [
        path
        for path in sorted(candidates, key=lambda p: (-candidates[p], p))
        if depth(path) <= max_part_depth
    ]
    chosen: list[str] = []

    # A child can outnumber its parent (an array's elements), so it waits for the parent.
    while len(chosen) < max_parts:
        admitted = set(chosen)
        ready = next((p for p in ordered if parent(p) is None or parent(p) in admitted), None)

        if ready is None:
            break

        chosen.append(ready)
        ordered.remove(ready)

    return tuple(chosen)


def pools_floats(path: str, classification: str, sql_type: str) -> bool:
    """Whether a part is an array's floating-point elements, which list no values (SPEC 2.2.18)."""

    return classification == "numeric" and path.endswith(ELEMENT) and is_floating_type(sql_type)


def _spelled(step: Step) -> str:
    if step.kind == "element":
        return ELEMENT

    if step.kind == "keys":
        return KEYS

    return member(step.name or "")
