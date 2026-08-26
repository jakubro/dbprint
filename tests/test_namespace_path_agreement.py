"""Every MockTable's namespace_path agrees with the FQN key it is filed under.

A real adapter derives both from the catalog, so a hand-written mismatch stands up a state no
adapter can produce. Walks tests/ with ast rather than importing - most MockTable sites need a
live pytest context to construct.
"""

from __future__ import annotations

import ast
from pathlib import Path


_TESTS_ROOT = Path(__file__).parent


def test_every_mocktable_namespace_path_matches_its_fqn_key() -> None:
    mismatches = [
        m for path in sorted(_TESTS_ROOT.rglob("*.py")) for m in _mismatches_in_file(path)
    ]

    assert not mismatches, "namespace_path disagrees with its FQN key:\n" + "\n".join(mismatches)


def _mismatches_in_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    bindings = _bindings(tree)
    mismatches: list[str] = []

    def _check(fqn: str, value: ast.expr) -> None:
        call = _as_mocktable_call(value)
        if call is None and isinstance(value, ast.Name):
            call = bindings.get(value.id)
        if call is None:
            return

        namespace_path = _namespace_path_literal(call)
        if namespace_path is None or namespace_path == tuple(fqn.split(".")):
            return
        mismatches.append(
            f"{path}:{call.lineno}: {fqn!r} keyed but namespace_path={namespace_path!r}",
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    _check(key.value, value)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].slice, ast.Constant)
            and isinstance(node.targets[0].slice.value, str)
        ):
            _check(node.targets[0].slice.value, node.value)

    return mismatches


def _bindings(tree: ast.Module) -> dict[str, ast.Call]:
    """Map each `name = MockTable(...)` local variable to the call it was bound to."""

    bindings: dict[str, ast.Call] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            continue
        call = _as_mocktable_call(node.value)
        if call is not None:
            bindings[node.targets[0].id] = call
    return bindings


def _namespace_path_literal(call: ast.Call) -> tuple[str, ...] | None:
    """Return the literal namespace_path tuple a MockTable call declares, if statically known."""

    for kw in call.keywords:
        if kw.arg != "namespace_path" or not isinstance(kw.value, ast.Tuple):
            continue
        segments: list[str] = []
        for element in kw.value.elts:
            if not (isinstance(element, ast.Constant) and isinstance(element.value, str)):
                return None
            segments.append(element.value)
        return tuple(segments)
    return None


def _as_mocktable_call(value: ast.expr) -> ast.Call | None:
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "MockTable"
    ):
        return value
    return None
