"""Every print artifact is parsed through `spec/artifact_yaml.py`, never PyYAML's own loaders.

`config/` reads user-authored files, not artifacts, and keeps PyYAML's error marks for its messages.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dbprint


_SRC = Path(dbprint.__file__).parent
_EXEMPT = ("config/", "spec/artifact_yaml.py")


def test_no_module_parses_yaml_outside_the_shared_loader() -> None:
    hits = [
        f"{relative}:{line} {name}"
        for path in sorted(_SRC.rglob("*.py"))
        if not (relative := path.relative_to(_SRC).as_posix()).startswith(_EXEMPT)
        for line, name in _pyyaml_loads(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert hits == []


def test_the_sweep_flags_what_it_exists_to_catch() -> None:
    snippet = ast.parse(
        "import yaml\n"
        "from yaml import CSafeLoader\n"
        "a = yaml.safe_load(t)\n"
        "b = yaml.load(t, Loader=yaml.CSafeLoader)\n"
        "class L(yaml.SafeLoader):\n    pass\n"
        "c = yaml.safe_dump(d)\n"
        "e = yaml.YAMLError\n",
    )

    assert _pyyaml_loads(snippet) == [
        (2, "CSafeLoader"),
        (3, "safe_load"),
        (4, "CSafeLoader"),
        (4, "load"),
        (5, "SafeLoader"),
    ]


def _pyyaml_loads(tree: ast.AST) -> list[tuple[int, str]]:
    hits = [
        (node.lineno, node.attr)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "yaml"
        and "load" in node.attr.lower()
    ]
    hits += [
        (node.lineno, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "yaml"
        for alias in node.names
        if "load" in alias.name.lower()
    ]

    return sorted(hits)
