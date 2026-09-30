"""Every number a renderer shows goes through `spell_number`/`spell_percent`, never its own format.

Layout geometry is exempt: by name in Python, by `style` attribute in the templates.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import dbprint


_SRC = Path(dbprint.__file__).parent

_RENDERERS = (
    "assertions/sql.py",
    "cli/rendering/check_tty.py",
    "cli/rendering/diff_data.py",
    "docs/view.py",
    "docs/web.py",
    "engine/context_assembler.py",
    "engine/notes_synthesis.py",
    "spec/predicate.py",
    "spec/scope.py",
)

_NOT_A_DISPLAYED_NUMBER = {
    ("docs/view.py", "cardinality_view"): "a bar height in a style attribute",
    ("docs/view.py", "pos"): "a box-plot label position",
    ("docs/view.py", "skyline_bar"): "a skyline bar height",
    ("docs/view.py", "skyline_heights"): "skyline bar heights",
    ("docs/view.py", "values_view"): "a value bar width",
    ("docs/web.py", "_relative_time"): "an elapsed duration, not a statistic",
    ("engine/notes_synthesis.py", "_non_null_total"): "an estimated count, not a display",
}

_TEMPLATE_FORMATTER = re.compile(r"\|\s*(round|format|human)\b|'%")
_EXPRESSION = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_STYLE_ATTRIBUTE = re.compile(r'style="[^"]*"')


def test_no_renderer_formats_a_number_itself() -> None:
    sites = {
        (module, function): line
        for module in _RENDERERS
        for line, function in _formatters(ast.parse((_SRC / module).read_text(encoding="utf-8")))
    }

    unexplained = {
        f"{m}:{line} {fn}"
        for (m, fn), line in sites.items()
        if (m, fn) not in _NOT_A_DISPLAYED_NUMBER
    }

    assert unexplained == set()
    assert set(_NOT_A_DISPLAYED_NUMBER) - set(sites) == set(), "stale allowlist entries"


def test_no_template_formats_a_number_outside_a_style_attribute() -> None:
    hits = [
        f"{path.name}: {expression}"
        for path in sorted((_SRC / "docs" / "templates").glob("*.html"))
        for expression in _template_formatters(path.read_text(encoding="utf-8"))
    ]

    assert hits == []


def test_the_sweep_flags_what_it_exists_to_catch() -> None:
    tree = ast.parse(
        "def a(n):\n"
        "    return f'{n:,}'\n"
        "def b(r):\n"
        "    return round(r * 100, 1)\n"
        "def c(r):\n"
        "    return '%.1f' % r\n"
        "def d(r):\n"
        "    return math.floor(r)\n",
    )
    template = '<b>{{ x|round(1) }}</b><i style="width:{{ x|round(1) }}%">{{ y|human }}</i>'

    assert {function for _, function in _formatters(tree)} == {"a", "b", "c", "d"}
    assert _template_formatters(template) == ["x|round(1)", "y|human"]


def _template_formatters(text: str) -> list[str]:
    return [
        expression.strip()
        for expression in _EXPRESSION.findall(_STYLE_ATTRIBUTE.sub("", text))
        if _TEMPLATE_FORMATTER.search(expression)
    ]


def _formatters(tree: ast.AST) -> list[tuple[int, str]]:
    return [
        (node.lineno, _enclosing(tree, node.lineno))
        for node in ast.walk(tree)
        if isinstance(node, ast.FormattedValue | ast.BinOp | ast.Call) and _formats(node)
    ]


def _formats(node: ast.FormattedValue | ast.BinOp | ast.Call) -> bool:
    if isinstance(node, ast.FormattedValue):
        return node.format_spec is not None

    if isinstance(node, ast.BinOp):
        return (
            isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
        )

    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else None
    )

    return name in ("round", "format", "floor")


def _enclosing(tree: ast.AST, line: int) -> str:
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]

    return max(functions, key=lambda f: f.lineno).name if functions else "<module>"
