"""--help is the command-surface source of truth.

Structural assertions on every command's help, plus a golden test that fails when
docs/CLI.md drifts from freshly-rendered --help.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from click.testing import CliRunner

from dbprint.cli.main import main


def _load_generator():
    """Import scripts/gen_cli_docs.py so the test shares the generator's render path."""

    path = Path(__file__).resolve().parents[2] / "scripts" / "gen_cli_docs.py"
    spec = importlib.util.spec_from_file_location("gen_cli_docs", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


gen = _load_generator()

COMMANDS = ("init", "generate", "diff", "list", "check", "context", "serve")
CONN_COMMANDS = ("generate", "diff", "list", "check", "context", "serve")

# A purpose phrase that only the expanded (multi-line) docstring carries.
PURPOSE = {
    "init": "Idempotent",
    "generate": "isolated so one failure",
    "diff": "read-only",
    "list": "freshness buckets",
    "check": "CI gate",
    "context": "prompt-ready",
    "serve": "Model Context Protocol",
}

# Cryptic options that must carry an inline, concrete example value.
CRYPTIC_EXAMPLES = {
    "check": ("--max-age", "7d"),
    "diff": ("--threshold", "0.05"),
    "context": ("--budget", "4000"),
    "generate": ("--include", "public.*"),
}


def _flat(*args: str) -> str:
    """Render --help with whitespace collapsed so substring checks are wrap-immune."""

    output = (
        CliRunner().invoke(main, [*args, "--help"], env={"NO_COLOR": "1", "TERM": "dumb"}).output
    )

    return " ".join(output.split())


class TestRootHelp:
    def test_lists_all_seven_commands(self) -> None:
        flat = _flat()

        for name in COMMANDS:
            assert name in flat

    def test_has_workflow_section(self) -> None:
        flat = _flat()
        assert "Typical workflow" in flat
        assert "SPEC.md" in flat


class TestCommandSurface:
    @pytest.mark.parametrize("command", COMMANDS)
    def test_has_substantive_docstring(self, command: str) -> None:
        assert PURPOSE[command] in _flat(command)

    @pytest.mark.parametrize("command", COMMANDS)
    def test_has_examples_and_exit_codes_sections(self, command: str) -> None:
        flat = _flat(command)
        assert "Examples:" in flat
        assert "Exit codes:" in flat

    @pytest.mark.parametrize("command", CONN_COMMANDS)
    def test_documents_conn_argument(self, command: str) -> None:
        assert "resolved from .dbprint.yaml" in _flat(command)

    @pytest.mark.parametrize("command", sorted(CRYPTIC_EXAMPLES))
    def test_cryptic_option_carries_example(self, command: str) -> None:
        option, example = CRYPTIC_EXAMPLES[command]
        flat = _flat(command)
        assert option in flat
        assert example in flat


class TestContextArgument:
    def test_documents_target_forms(self) -> None:
        flat = _flat("context")
        assert "TARGET" in flat
        assert "table FQN" in flat
        assert "fnmatch pattern" in flat
        assert "--all for every table" in flat


class TestGoldenReference:
    def test_committed_reference_matches_rendered_help(self) -> None:
        committed = gen.DOCS_PATH.read_text()
        assert committed == gen.build_document(), (
            "docs/CLI.md is out of date with --help. Run `just docs` and commit the result."
        )

    def test_golden_check_detects_drift(self, monkeypatch: pytest.MonkeyPatch) -> None:
        committed = gen.DOCS_PATH.read_text()
        monkeypatch.setattr(main.commands["check"], "help", "DRIFT-MARKER-XYZ")

        rendered = gen.build_document()

        assert rendered != committed
