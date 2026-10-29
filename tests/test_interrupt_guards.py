"""An interrupt reaches the run however a driver reports it: nothing catches one, and every command
that issues statements runs them inside the interruptible span.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dbprint


_SRC = Path(dbprint.__file__).parent

_MAY_CATCH_AN_INTERRUPT = {
    (
        "adapters/duckdb/connection.py",
        "execute",
    ): "interrupts the statement in flight, then raises the driver's error from the interrupt",
    (
        "adapters/snowflake/connection.py",
        "execute",
    ): "cancels the session's statement in flight, then raises the driver's error from the interrupt",
    (
        "adapters/snowflake/connection.py",
        "run",
    ): "hands whatever ended the statement, a connector panic included, to the waiting thread",
    (
        "cli/commands/docs.py",
        "serve_command",
    ): "`docs serve` stops on Ctrl-C and issues no statement",
}


def test_no_except_clause_catches_an_interrupt() -> None:
    sites = {
        (path.relative_to(_SRC).as_posix(), function)
        for path in sorted(_SRC.rglob("*.py"))
        for function in _interrupt_catches(ast.parse(path.read_text(encoding="utf-8")))
    }

    assert sites - set(_MAY_CATCH_AN_INTERRUPT) == set()
    assert set(_MAY_CATCH_AN_INTERRUPT) - sites == set(), "stale allowlist entries"


def test_every_command_building_an_engine_runs_inside_the_interruptible_span() -> None:
    builders = {
        path.name
        for path in sorted((_SRC / "cli" / "commands").glob("*.py"))
        if "build_engine" in _called(ast.parse(path.read_text(encoding="utf-8")))
    }
    covered = {
        path.name
        for path in sorted((_SRC / "cli" / "commands").glob("*.py"))
        if "interruptible" in _called(ast.parse(path.read_text(encoding="utf-8")))
    }

    assert builders
    assert builders - covered == set()


def test_the_sweep_flags_what_it_exists_to_catch() -> None:
    tree = ast.parse(
        "def a():\n"
        "    try:\n"
        "        pass\n"
        "    except BaseException:\n"
        "        pass\n"
        "def b():\n"
        "    try:\n"
        "        pass\n"
        "    except (ValueError, KeyboardInterrupt):\n"
        "        pass\n"
        "def c():\n"
        "    try:\n"
        "        pass\n"
        "    except:\n"
        "        pass\n"
        "def d():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n",
    )

    assert set(_interrupt_catches(tree)) == {"a", "b", "c"}


def _interrupt_catches(tree: ast.AST) -> list[str]:
    out: list[str] = []

    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
            continue

        for node in ast.walk(function):
            if isinstance(node, ast.ExceptHandler) and _catches_an_interrupt(node.type):
                out.append(function.name)

    return out


def _catches_an_interrupt(caught: ast.expr | None) -> bool:
    if caught is None:
        return True

    names = caught.elts if isinstance(caught, ast.Tuple) else [caught]

    return any(
        isinstance(name, ast.Name) and name.id in {"BaseException", "KeyboardInterrupt"}
        for name in names
    )


def _called(tree: ast.AST) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
