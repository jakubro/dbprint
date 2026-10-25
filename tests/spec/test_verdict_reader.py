"""One module decides whether a human verdict takes effect; every other reader goes through it."""

from __future__ import annotations

import ast
from pathlib import Path

import dbprint


_PACKAGE = Path(dbprint.__file__).parent

_READS_THE_KEY_FOR_ANOTHER_REASON = {
    (
        "engine/relationship_graph.py",
        "_authored_edges",
    ): "an entry carrying a verdict is never a human-added edge",
    (
        "conformance/relationship_annotations.py",
        "_check_one",
    ): "reports the verdict the shared rule voids",
}


def test_no_other_module_reads_a_verdict() -> None:
    hits = sorted(
        (path.relative_to(_PACKAGE).as_posix(), function.name)
        for path in _PACKAGE.rglob("*.py")
        if path.relative_to(_PACKAGE).as_posix() != "spec/edges.py"
        for function in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef) and _names_verdict(function)
    )

    assert hits == sorted(_READS_THE_KEY_FOR_ANOTHER_REASON)


def test_the_sweep_flags_what_it_exists_to_catch() -> None:
    tree = ast.parse(
        'def f(e):\n    """A verdict docstring."""\n    return e.get("verdict")\n'
        'def g(e):\n    """Mentions verdict only here."""\n    return e\n',
    )
    flagged = [
        f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and _names_verdict(f)
    ]

    assert flagged == ["f"]


def _names_verdict(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(node, ast.Constant) and node.value == "verdict" for node in ast.walk(function)
    )
