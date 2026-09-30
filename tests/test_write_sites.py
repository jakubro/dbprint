"""Every artifact write goes through the run stage, so none reaches the print root before commit.

Only `engine/staging.py` calls `write_atomic`; `os.replace` appears there and in the writer only.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dbprint


_SRC = Path(dbprint.__file__).parent


def test_write_atomic_is_called_only_by_the_stage() -> None:
    assert _call_sites("write_atomic") == {"engine/staging.py"}


def test_os_replace_is_called_only_by_the_writer_and_the_stage() -> None:
    assert _call_sites("replace", owner="os") == {"engine/staging.py", "engine/writer.py"}


def test_the_sweep_sees_a_planted_call() -> None:
    tree = ast.parse("import os\nos.replace(a, b)\nwrite_atomic(d, {})\n")

    assert _calls(tree, "replace", owner="os") and _calls(tree, "write_atomic")


def _call_sites(name: str, owner: str | None = None) -> set[str]:
    return {
        path.relative_to(_SRC).as_posix()
        for path in sorted(_SRC.rglob("*.py"))
        if _calls(ast.parse(path.read_text(encoding="utf-8")), name, owner)
    }


def _calls(tree: ast.AST, name: str, owner: str | None = None) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func

        if owner is None and isinstance(func, ast.Name) and func.id == name:
            return True

        if (
            isinstance(func, ast.Attribute)
            and func.attr == name
            and (owner is None or isinstance(func.value, ast.Name) and func.value.id == owner)
        ):
            return True

    return False
