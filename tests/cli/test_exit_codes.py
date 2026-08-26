"""The CLI's exit codes come from the vocabulary; every help section documents reachable ones.

`engine/result.py` is the only definition site, so this module scans the CLI for bare literals.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner

from dbprint.cli.main import main


CLI_ROOT = Path(__file__).resolve().parents[2] / "src" / "dbprint" / "cli"

# Names that hold an exit code; an integer assigned or compared to one is a literal.
_EXIT_NAMES = frozenset({"exit_code", "exit_codes", "overall_exit", "offline_exit", "top"})

COMMANDS = ["generate", "check", "diff", "context", "init", "list", "serve"]


def _cli_sources() -> list[Path]:
    return sorted(p for p in CLI_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _literal_exit_codes(path: Path) -> list[tuple[int, str]]:
    """Return (line, snippet) for every exit code written as an integer literal."""

    tree = ast.parse(path.read_text())
    hits: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func

            if (
                isinstance(func, ast.Attribute)
                and func.attr == "exit"
                and node.args
                and _is_int(node.args[0])
            ):
                hits.append((node.lineno, f"ctx.exit({ast.unparse(node.args[0])})"))

            for kw in node.keywords:
                if kw.arg in _EXIT_NAMES and _is_int(kw.value):
                    hits.append((node.lineno, f"{kw.arg}={ast.unparse(kw.value)}"))

        elif isinstance(node, ast.Assign):
            if any(_names_an_exit(t) for t in node.targets) and _is_int(node.value):
                hits.append((node.lineno, ast.unparse(node)))

        elif isinstance(node, ast.Compare):
            if _names_an_exit(node.left) and any(_is_int(c) for c in node.comparators):
                hits.append((node.lineno, ast.unparse(node)))

    return hits


def _is_int(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, int)


def _names_an_exit(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in _EXIT_NAMES

    if isinstance(node, ast.Attribute):
        return node.attr in _EXIT_NAMES

    return False


def _documented_codes(command: str) -> set[str]:
    """Exit codes the command's own --help advertises."""

    help_text = CliRunner().invoke(main, [command, "--help"]).output
    block = re.split(r"Exit codes:", help_text, maxsplit=1)

    assert len(block) == 2, f"{command} --help has no exit-code list"

    body = re.split(r"\n\s*\n", block[1].strip(), maxsplit=1)[0]

    return set(re.findall(r"[-•]\s*`?(\d)`?:", body))


class TestNoLiteralExitCodes:
    def test_the_cli_package_writes_no_exit_code_as_a_literal(self) -> None:
        offenders = {
            str(p.relative_to(CLI_ROOT)): hits
            for p in _cli_sources()
            if (hits := _literal_exit_codes(p))
        }

        assert offenders == {}, "Exit codes written as integer literals:\n" + "\n".join(
            f"  {rel}:{line}  {snippet}"
            for rel, hits in sorted(offenders.items())
            for line, snippet in hits
        )

    def test_the_scan_would_catch_a_literal(self, tmp_path: Path) -> None:
        """The guard is only worth having if it fails on the thing it forbids."""

        planted = tmp_path / "planted.py"
        planted.write_text("def f(ctx):\n    exit_code = 4\n    ctx.exit(1)\n")

        assert len(_literal_exit_codes(planted)) == 2


class TestHelpDocumentsReachableCodes:
    """Every code a command's `Exit codes:` help section lists must be one it can return.

    `REACHABLE` is read off each command's source.
    """

    REACHABLE: ClassVar[dict[str, set[str]]] = {
        "generate": {"0", "1", "3", "4", "5", "7"},
        "check": {"0", "1", "2", "3", "4", "5", "6"},
        "diff": {"0", "1", "4", "5"},
        "context": {"0", "1"},
        "init": {"0"},
        "list": {"0", "1"},
        "serve": {"0", "1"},
    }

    @pytest.mark.parametrize("command", COMMANDS)
    def test_exit_codes_section_matches_the_reachable_set(self, command: str) -> None:
        assert _documented_codes(command) == self.REACHABLE[command]
