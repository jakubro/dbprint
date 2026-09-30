"""Every consumer that reads the manifest's tables also asks which ones the last run could not
profile (SPEC 2.5), unless a reviewed reason says the listing never needs it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dbprint


_SRC = Path(dbprint.__file__).parent
_CONSUMERS = (
    *sorted((_SRC / "cli").rglob("*.py")),
    *sorted((_SRC / "mcp").rglob("*.py")),
    *sorted((_SRC / "docs").rglob("*.py")),
    _SRC / "engine" / "context_assembler.py",
    _SRC / "engine" / "thresholds.py",
)
_CONSULTS = frozenset({"failed_tables", "_absent_table"})

_NEEDS_NO_FAILURES = {
    ("cli/commands/check.py", "_judged_entries"): "judges the freshness of present entries",
    ("cli/commands/check.py", "_load_committed_statistics"): "reads one present table's files",
    ("cli/commands/check.py", "_summary_view"): "renders issues keyed by present tables",
    ("cli/commands/check.py", "_tables_with_errors"): "maps issues onto present tables",
    ("cli/commands/list_cmd.py", "_unwalkable_entry_reason"): "checks the entries' shape",
    ("engine/thresholds.py", "resolve"): "resolves thresholds for present entries",
    ("mcp/resources.py", "_enumerate_connection"): "lists files; an unprofiled table has none",
    ("mcp/tools.py", "_freshness"): "judges the freshness of present entries",
    ("mcp/tools.py", "_tool_search_columns"): "searches columns; an unprofiled table has none",
}


def test_every_table_listing_consults_the_failed_tables() -> None:
    readers = {
        (path.relative_to(_SRC).as_posix(), function.name): _consults(function)
        for path in _CONSUMERS
        for function in _functions(ast.parse(path.read_text(encoding="utf-8")))
        if _reads_tables(function)
    }

    unexplained = {
        site
        for site, consults in readers.items()
        if not consults and site not in _NEEDS_NO_FAILURES
    }

    assert unexplained == set()
    assert set(_NEEDS_NO_FAILURES) - set(readers) == set(), "stale allowlist entries"


def test_the_sweep_sees_a_planted_reader() -> None:
    tree = ast.parse(
        "def a(manifest):\n"
        "    return manifest['tables']\n"
        "def b(manifest):\n"
        "    return walkable_tables(manifest), failed_tables(manifest)\n"
        "def c(manifest):\n"
        "    return manifest.get('tables')\n",
    )

    assert {f.name for f in _functions(tree) if _reads_tables(f) and not _consults(f)} == {"a", "c"}


def _functions(tree: ast.AST) -> list[ast.FunctionDef]:
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]


def _reads_tables(function: ast.FunctionDef) -> bool:
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and _callee(node) == "walkable_tables":
            return True

        if (
            isinstance(node, ast.Call)
            and _callee(node) == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "tables"
        ):
            return True

        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Load)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "tables"
        ):
            return True

    return False


def _consults(function: ast.FunctionDef) -> bool:
    return any(
        isinstance(node, ast.Call) and _callee(node) in _CONSULTS for node in ast.walk(function)
    )


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id

    return call.func.attr if isinstance(call.func, ast.Attribute) else None
