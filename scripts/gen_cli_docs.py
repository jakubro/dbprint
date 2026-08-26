"""Render `dbprint --help` (root + every subcommand) into docs/CLI.md.

Deterministic - color disabled, width pinned. Run via `just docs`; golden-tested by
tests/cli/test_help.py so the file and the CLI cannot silently diverge.
"""

from __future__ import annotations

from pathlib import Path

import rich_click
from click.testing import CliRunner

from dbprint.cli.main import main


WIDTH = 100
DOCS_PATH = Path(__file__).resolve().parents[1] / "docs" / "CLI.md"

# Workflow order: scaffold -> profile -> inspect -> gate -> consume -> serve -> browse.
# "docs" is listed alongside its subcommands so both levels of --help are captured.
COMMANDS = (
    "init",
    "generate",
    "diff",
    "list",
    "check",
    "context",
    "serve",
    "docs",
    "docs serve",
    "docs build",
)

_HEADER = """\
# dbprint CLI reference

Complete `--help` for every command, captured verbatim. This file is generated
from the CLI itself - do not edit it by hand. Run `just docs` to regenerate it
after changing a command's docstring, options, or help sections.
"""


def build_document() -> str:
    """Return the full text of the generated CLI reference file."""

    sections = [_section("dbprint", ["--help"])]
    sections.extend(_section(f"dbprint {name}", [*name.split(), "--help"]) for name in COMMANDS)

    return _HEADER + "\n" + "\n\n".join(sections) + "\n"


def write_document() -> None:
    """Render the reference and write it to DOCS_PATH, creating parent dirs."""

    DOCS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOCS_PATH.write_text(build_document())


def _section(title: str, args: list[str]) -> str:
    """Render one command's help as a Markdown heading + fenced block."""

    return f"## `{title}`\n\n```text\n{_render_help(args)}\n```"


def _render_help(args: list[str]) -> str:
    """Invoke `--help` for `args` with color off and width pinned.

    Saves/restores rich-click's global config so this cannot leak into other code sharing
    the process (the golden test runs in-suite); strips the trailing padding too.
    """

    cfg = rich_click.rich_click
    saved = (cfg.COLOR_SYSTEM, cfg.WIDTH, cfg.MAX_WIDTH)

    try:
        cfg.COLOR_SYSTEM = None
        cfg.WIDTH = WIDTH
        cfg.MAX_WIDTH = WIDTH
        result = CliRunner().invoke(
            main,
            args,
            env={"COLUMNS": str(WIDTH), "NO_COLOR": "1", "TERM": "dumb"},
        )
    finally:
        cfg.COLOR_SYSTEM, cfg.WIDTH, cfg.MAX_WIDTH = saved

    lines = [line.rstrip() for line in result.output.splitlines()]

    return "\n".join(lines).strip("\n")


if __name__ == "__main__":
    write_document()
    print(f"wrote {DOCS_PATH}")
