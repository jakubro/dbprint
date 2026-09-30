"""The run path reads the committed print only through `engine/carried.py`.

A second parse of the committed files anywhere in the engine fails here, named by module and line.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dbprint


_ENGINE = Path(dbprint.__file__).parent / "engine"

_RUN_PATH = ("orchestrator.py", "manifest_builder.py", "diff.py")

# Parse the statistics text the run itself just dumped, not a committed file.
_OWN_OUTPUT_READERS = frozenset({"_reread_statistics", "_rewrite_statistics"})
_YAML_PARSES = frozenset({"safe_load", "safe_load_all", "load", "load_all"})


def test_no_run_path_module_walks_a_manifest_tables_map() -> None:
    hits = [hit for module in _RUN_PATH for hit in _tables_reads(module, _parse(module))]

    assert hits == []


def test_no_run_path_module_parses_yaml_outside_its_own_output() -> None:
    hits = [hit for module in _RUN_PATH for hit in _yaml_loads(module, _parse(module))]

    assert hits == []


def test_the_carried_model_owns_the_age_clock() -> None:
    callers = [
        f"{path.name}:{node.lineno}"
        for path in sorted(_ENGINE.glob("*.py"))
        if path.name not in {"freshness.py", "carried.py"}
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and _called_name(node) == "age_days"
    ]

    assert callers == []


def test_the_sweeps_flag_what_they_exist_to_catch() -> None:
    snippet = ast.parse(
        "def f(m):\n    x = m['tables']\n    y = m.get('tables')\n    return yaml.safe_load(x)\n"
        "def g(p):\n    return artifact_yaml.load(p.read_text())\n",
    )

    assert len(_tables_reads("snippet", snippet)) == 2
    assert len(_yaml_loads("snippet", snippet)) == 2


def test_the_parse_sweep_reaches_module_level_and_async_code() -> None:
    snippet = ast.parse(
        "COMMITTED = yaml.safe_load(PATH.read_text())\n"
        "async def f(p):\n    return yaml.safe_load(p.read_text())\n",
    )

    assert _yaml_loads("snippet", snippet) == ["snippet:1", "snippet:3"]


def _parse(module: str) -> ast.Module:
    return ast.parse((_ENGINE / module).read_text(encoding="utf-8"))


def _tables_reads(module: str, tree: ast.Module) -> list[str]:
    hits: list[str] = []

    for node in ast.walk(tree):
        subscript = (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and _is_tables(node.slice)
        )
        getter = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and bool(node.args)
            and _is_tables(node.args[0])
        )

        if subscript or getter:
            hits.append(f"{module}:{node.lineno}")

    return hits


def _yaml_loads(module: str, tree: ast.Module) -> list[str]:
    own = {
        id(node)
        for function in ast.walk(tree)
        if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef)
        and function.name in _OWN_OUTPUT_READERS
        for node in ast.walk(function)
    }

    return [
        f"{module}:{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_yaml_parse(node) and id(node) not in own
    ]


def _is_yaml_parse(node: ast.Call) -> bool:
    func = node.func

    if isinstance(func, ast.Name):
        return func.id in {"safe_load", "safe_load_all"}

    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id in {"yaml", "artifact_yaml"}
        and func.attr in _YAML_PARSES
    )


def _is_tables(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value == "tables"


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id

    if isinstance(node.func, ast.Attribute):
        return node.func.attr

    return None
