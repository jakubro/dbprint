"""dbprint CLI root - rich-click group wiring the dbprint subcommands."""

from __future__ import annotations

from typing import Any

import rich_click as click
from click.exceptions import Abort, ClickException, Exit

from dbprint import __version__
from dbprint.engine import EXIT_GENERIC
from .commands.check import check_command
from .commands.context import context_command
from .commands.diff import diff_command
from .commands.docs import docs_group
from .commands.generate import generate_command
from .commands.init import init_command
from .commands.list_cmd import list_command
from .commands.serve import serve_command


# Render every command's help as Markdown for consistent paragraph/list formatting.
click.rich_click.TEXT_MARKUP = "markdown"


class _RootGroup(click.RichGroup):
    """Turn an uncaught subcommand error into a one-line stderr cause; `--debug` re-raises."""

    def invoke(self, ctx: click.Context) -> Any:
        try:
            return super().invoke(ctx)
        except (ClickException, Exit, Abort, SystemExit):
            raise
        except Exception as exc:
            if ctx.params.get("debug"):
                raise

            click.echo(f"error: {exc}", err=True)
            ctx.exit(EXIT_GENERIC)


@click.group(
    name="dbprint",
    cls=_RootGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, prog_name="dbprint")
@click.option("--debug", is_flag=True, default=False, help="Print full tracebacks on error.")
@click.pass_context
def main(ctx: click.Context, debug: bool) -> None:
    """dbprint - offline database prints (DDL + column statistics) for AI agents.

    A print is a portable, git-committed snapshot of a database's structure and
    column-level data distributions, consumable offline by humans, AI coding
    agents, and CI.

    The commands below scaffold a project, profile the database, and verify or
    consume the committed prints.

    **Typical workflow:** `init` -> `generate` -> `diff` (ad-hoc) or `check`
    (CI gate).

    Run `dbprint COMMAND --help` for per-command usage. The on-disk format is
    specified in `SPEC.md`, which ships inside the package and is published at
    https://github.com/jakubro/dbprint/blob/main/docs/format/v1/SPEC.md
    """

    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug


main.add_command(init_command)
main.add_command(generate_command)
main.add_command(list_command)
main.add_command(context_command)
main.add_command(check_command)
main.add_command(diff_command)
main.add_command(serve_command)
main.add_command(docs_group)
