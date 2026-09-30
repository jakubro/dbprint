"""Every sort over a value list's count and value goes through the one SPEC 2.2.4 key.

A new site spelling its own tie-break fails here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dbprint


_SRC = Path(dbprint.__file__).parent
_ALLOWED = {
    ("adapters/base.py", "order_values"),
    ("conformance/statistics.py", "_check_value_order"),
    ("engine/orchestrator.py", "_value_entries"),
}


def test_value_lists_are_ordered_only_through_the_shared_key() -> None:
    sites = {
        (path.relative_to(_SRC).as_posix(), function)
        for path in sorted(_SRC.rglob("*.py"))
        for function in _value_sorts(ast.parse(path.read_text(encoding="utf-8")))
    }

    assert ("adapters/base.py", "order_values") in sites
    assert sites <= _ALLOWED


def test_the_sweep_sees_a_planted_sort() -> None:
    tree = ast.parse(
        "def f(xs):\n"
        "    xs.sort(key=lambda v: (-v.count, str(v.value)))\n"
        "def g(xs):\n"
        "    return sorted(xs, key=lambda e: (-e['count'], e['value']))\n",
    )

    assert _value_sorts(tree) == {"f", "g"}


def _value_sorts(tree: ast.AST) -> set[str]:
    return {
        function.name
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        for call in ast.walk(function)
        if isinstance(call, ast.Call) and _is_sort(call) and _keys_on_count_and_value(call)
    }


def _is_sort(call: ast.Call) -> bool:
    func = call.func

    return (isinstance(func, ast.Name) and func.id == "sorted") or (
        isinstance(func, ast.Attribute) and func.attr == "sort"
    )


def _keys_on_count_and_value(call: ast.Call) -> bool:
    key = next((k.value for k in call.keywords if k.arg == "key"), None)

    if not isinstance(key, ast.Lambda):
        return False

    names = {node.attr for node in ast.walk(key.body) if isinstance(node, ast.Attribute)} | {
        node.value for node in ast.walk(key.body) if isinstance(node, ast.Constant)
    }

    return {"count", "value"} <= names
