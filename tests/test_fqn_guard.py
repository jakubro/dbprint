"""Every FQN is built and taken apart through `spec/fqn.py`, the one spelling of its separator.

Dot splits, joins, dot-to-slash rewrites and dotted f-strings are flagged; each exception says why.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dbprint


_SRC = Path(dbprint.__file__).parent

_NOT_AN_FQN = {
    ("adapters/base.py", "member_source"): "a SQL record member reference",
    ("adapters/bigquery/ddl.py", "extract_ddl"): "a quoted SQL reference",
    ("adapters/bigquery/introspect.py", "_info_schema"): "a quoted SQL reference",
    ("adapters/databricks/introspect.py", "_uc_list_candidates"): "an error message",
    ("adapters/identifiers.py", "_column_message"): "a column-grain display name",
    ("adapters/identifiers.py", "qualified"): "a qualified SQL column reference",
    ("adapters/identifiers.py", "quote_path"): "a quoted SQL reference",
    ("adapters/mock.py", "compute_normalized_cardinality"): "an error message",
    ("adapters/snowflake/introspect.py", "physical_layout"): "a quoted SQL reference",
    ("assertions/statistic.py", "_column_path"): "a finding path",
    ("cli/commands/check.py", "_drift_issues_from"): "an issue path",
    ("cli/commands/check.py", "_fault_issues"): "an issue path",
    ("cli/rendering/diff_data.py", "_relationship_lines_by_table"): "a column-grain display name",
    ("cli/rendering/errors.py", "sketch_failure_texts"): "a column-grain display name",
    ("cli/rendering/progress.py", "_connection_summary"): "a column-grain display name",
    ("config/project.py", "_coerce_pattern_list"): "a config key path in a message",
    ("config/project.py", "_coerce_stat_change_threshold"): "a config key path in a message",
    ("config/project.py", "_coerce_vocabulary"): "a config key path in a message",
    ("conformance/diff.py", "check_redacted_values"): "a statistic path",
    ("engine/carried.py", "redaction_mismatches"): "a column-grain display name",
    ("engine/context_assembler.py", "fk_target_map"): "a column-grain display name",
    ("engine/context_assembler.py", "_markdown_joins"): "a column-grain display name",
    ("engine/context_assembler.py", "_markdown_relationships"): "a column-grain display name",
    ("engine/diff.py", "_diff_one_column_stats"): "a statistic path",
    ("engine/diff.py", "_get_path"): "a statistic path",
    ("engine/diff.py", "_stat_paths"): "a statistic path",
    ("engine/orchestrator.py", "_detect_columns"): "a column-grain display name",
    ("engine/orchestrator.py", "_enriched_part"): "a column-grain display name",
    ("spec/absence.py", "_column_head"): "a field path",
    ("spec/absence.py", "_lookup"): "a field path",
    ("spec/absence.py", "_table_head"): "a field path",
    ("spec/absence.py", "read_column_field"): "a field path",
    ("spec/drift.py", "rule_for"): "a field path",
    ("spec/looks_like.py", "_match_jwt"): "a JWT's dot-separated parts",
    ("spec/predicate.py", "resolve_edge_stat"): "a statistic path",
    ("spec/rounding.py", "__init__"): "a Python type's qualified name",
    ("spec/value_text.py", "_clock"): "a clock's fractional seconds",
}


def test_no_module_spells_the_fqn_separator_by_hand() -> None:
    sites = {
        (path.relative_to(_SRC).as_posix(), function): line
        for path in sorted(_SRC.rglob("*.py"))
        if path.relative_to(_SRC).as_posix() != "spec/fqn.py"
        for line, function in _hand_rolled(ast.parse(path.read_text(encoding="utf-8")))
    }

    assert {
        f"{path}:{line} {fn}" for (path, fn), line in sites.items() if (path, fn) not in _NOT_AN_FQN
    } == set()
    assert set(_NOT_AN_FQN) - set(sites) == set(), "stale allowlist entries"


def test_the_sweep_flags_what_it_exists_to_catch() -> None:
    tree = ast.parse(
        "def a(fqn):\n"
        "    return fqn.split('.')\n"
        "def b(parts):\n"
        "    return '.'.join(parts)\n"
        "def c(fqn):\n"
        "    return fqn.replace('.', '/')\n"
        "def d(schema, table):\n"
        "    return f'{schema}.{table}'\n"
        "def e(fqn):\n"
        "    return fqn.rpartition('.')\n",
    )

    assert {function for _, function in _hand_rolled(tree)} == {"a", "b", "c", "d", "e"}


def _hand_rolled(tree: ast.AST) -> list[tuple[int, str]]:
    return [
        (node.lineno, _enclosing(tree, node.lineno))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call | ast.JoinedStr) and _spells_separator(node)
    ]


def _spells_separator(node: ast.Call | ast.JoinedStr) -> bool:
    if isinstance(node, ast.JoinedStr):
        values = node.values

        return any(
            isinstance(separator := values[i], ast.Constant)
            and separator.value == "."
            and isinstance(values[i - 1], ast.FormattedValue)
            and isinstance(values[i + 1], ast.FormattedValue)
            for i in range(1, len(values) - 1)
        )

    if not isinstance(node.func, ast.Attribute):
        return False

    method, args = node.func.attr, node.args
    dot_first = bool(args) and isinstance(args[0], ast.Constant) and args[0].value == "."

    if method in ("split", "rsplit", "partition", "rpartition"):
        return dot_first

    if method == "join":
        return isinstance(node.func.value, ast.Constant) and node.func.value.value == "."

    if method == "replace":
        return dot_first and isinstance(args[1], ast.Constant) and args[1].value == "/"

    return False


def _enclosing(tree: ast.AST, line: int) -> str:
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]

    return max(functions, key=lambda f: f.lineno).name if functions else "<module>"
