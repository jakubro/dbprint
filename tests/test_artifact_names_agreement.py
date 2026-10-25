"""A manifest entry's artifact names are read only through `declared_artifacts` (SPEC 2.5).

Reading the mapping directly opens whatever name the print supplies, `../../x` included.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dbprint


_SRC = Path(dbprint.__file__).parent
_OWNER = _SRC / "spec" / "artifacts.py"

_READS_KEYS_ONLY = {
    ("engine/carried.py", "_entry_from_payload"): "reads which kinds are declared, never a name",
}


def test_no_module_reads_an_entrys_artifacts_by_hand() -> None:
    offenders = {
        (path.relative_to(_SRC).as_posix(), function.name)
        for path in sorted(_SRC.rglob("*.py"))
        if path != _OWNER
        for function in _functions(ast.parse(path.read_text(encoding="utf-8")))
        if _reads_artifacts(function)
    }

    assert offenders - set(_READS_KEYS_ONLY) == set()
    assert set(_READS_KEYS_ONLY) <= offenders, "a listed reader no longer reads the mapping"


def test_the_sweep_sees_a_planted_reader() -> None:
    tree = ast.parse(
        "def reader(entry):\n"
        "    return entry.get('artifacts')['ddl']\n"
        "def subscripted(entry):\n"
        "    return entry['artifacts']['ddl']\n",
    )

    assert [f.name for f in _functions(tree) if _reads_artifacts(f)] == ["reader", "subscripted"]


def _functions(tree: ast.AST) -> list[ast.FunctionDef]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]


def _reads_artifacts(function: ast.FunctionDef) -> bool:
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "artifacts"
        ):
            return True

        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "artifacts"
        ):
            return True

    return False
