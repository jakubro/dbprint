"""Every reader decides what a scoped file covers through `spec.scope`.

An AST walk fails a by-key read of `scope`/`rows_scanned` outside the resolvers.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dbprint


_PACKAGE = Path(dbprint.__file__).parent

_SWEPT = ("assertions", "cli", "docs", "engine", "mcp")

_KEYS = frozenset({"scope", "rows_scanned", "scope.rows_scanned"})

_RESOLVERS = frozenset({"column_value", "block_value", "read_column_field", "read_table_block"})

_NOT_READERS = {
    "engine/orchestrator.py": "producer",
    "engine/writer.py": "producer",
    "engine/manifest_builder.py": "producer",
    "engine/carried.py": "hydrates the committed artifact verbatim for the producer",
    "engine/diff.py": "comparability between two reads, not a statement to a reader",
    "engine/baseline.py": "comparability between two reads, not a statement to a reader",
}

_READS_ANOTHER_MAPPING: dict[tuple[str, str], str] = {
    ("docs/view.py", "row_count_view"): "reads scope_view's own rendered mapping",
}


def test_no_consumer_reads_the_scope_itself() -> None:
    assert set(_bypasses()) - set(_READS_ANOTHER_MAPPING) == set()


def test_every_allowlisted_read_still_exists() -> None:
    assert set(_READS_ANOTHER_MAPPING) - set(_bypasses()) == set()


def test_the_sweep_flags_what_it_exists_to_catch() -> None:
    snippet = ast.parse(
        "def f(stats, col):\n"
        "    block = stats.get('scope')\n"
        "    coverage = col.get('values_coverage')\n"
        "    return block, coverage == 1.0, column_value(col, 'rows_scanned')\n",
    )

    assert len(_hits(snippet)) == 3


def _bypasses() -> list[tuple[str, str]]:
    found = set()

    for directory in _SWEPT:
        for path in (_PACKAGE / directory).rglob("*.py"):
            rel = path.relative_to(_PACKAGE).as_posix()

            if rel not in _NOT_READERS:
                found |= {(rel, name) for name in _hits(ast.parse(path.read_text("utf-8")))}

    return sorted(found)


def _hits(tree: ast.AST) -> list[str]:
    out = []

    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        aliases = _coverage_aliases(function)
        out.extend(function.name for node in ast.walk(function) if _reads(node, aliases))

    return out


def _reads(node: ast.AST, aliases: set[str]) -> bool:
    if isinstance(node, ast.Compare):
        if not any(isinstance(op, (ast.Eq, ast.NotEq, ast.GtE)) for op in node.ops):
            return False

        sides = [node.left, *node.comparators]

        return any(_is_one(side) for side in sides) and any(
            _is_coverage(side, aliases) for side in sides
        )

    return _key_of(node) in _KEYS


def _key_of(node: ast.AST) -> object:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ):
        return node.args[0].value

    if isinstance(node, ast.Call) and _callee(node) in _RESOLVERS and len(node.args) > 1:
        return node.args[1].value if isinstance(node.args[1], ast.Constant) else None

    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
        return node.slice.value if isinstance(node.slice, ast.Constant) else None

    return None


def _coverage_aliases(function: ast.AST) -> set[str]:
    return {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign) and _names_coverage(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _is_coverage(node: ast.AST, aliases: set[str]) -> bool:
    if isinstance(node, ast.Attribute):
        node = node.value

    return (isinstance(node, ast.Name) and node.id in aliases) or _names_coverage(node)


def _names_coverage(node: ast.AST) -> bool:
    return any(
        isinstance(inner, ast.Constant) and inner.value == "values_coverage"
        for inner in ast.walk(node)
    )


def _is_one(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value == 1.0 and not isinstance(node.value, bool)


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id

    return call.func.attr if isinstance(call.func, ast.Attribute) else None
