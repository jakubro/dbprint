"""`dbprint annotate` - prints one table's annotation-authoring brief, or checks its annotations."""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

import rich_click as click
from rich.console import Console

from dbprint.config import ConnectionConfig
from dbprint.config.resolution import ConnectionResolutionError, resolve
from dbprint.conformance import Issue, issues_for_table, validate_print
from dbprint.engine import (
    EXIT_GENERIC,
    EXIT_OK,
    AssemblyOptions,
    assemble_context,
    open_questions,
)
from dbprint.engine.annotate_skill import annotate_skill_steps
from dbprint.spec.artifacts import (
    RELATIONSHIPS_ANNOTATIONS_FILENAME,
    STATISTICS_ANNOTATIONS_FILENAME,
)
from ..options import project_option, resolve_project
from ..rendering import resolve_render_mode
from ..rendering.annotate_brief import render_brief
from ..rendering.context_tty import render_human
from ..selection import resolve_connections


_ANNOTATION_FILES = frozenset({STATISTICS_ANNOTATIONS_FILENAME, RELATIONSHIPS_ANNOTATIONS_FILENAME})


@click.command(name="annotate")
@click.argument("table")
@click.argument("connection", required=False)
@project_option
@click.option(
    "--check",
    "check",
    is_flag=True,
    default=False,
    help="List this table's annotation findings instead of printing the brief; exit 1 if there "
    "are any.",
)
@click.option(
    "--tui/--no-tui",
    default=None,
    help="Force TTY (syntax-highlighted) or piped (plain-text) rendering of the brief.",
)
@click.pass_context
def annotate_command(
    ctx: click.Context,
    table: str,
    connection: str | None,
    project: str | None,
    check: bool,
    tui: bool | None,
) -> None:
    """Print an authoring brief for one table's annotations, or check them.

    The brief is the annotation skill's steps, the table's `--purpose query` context, and the
    open questions the print measured but cannot answer - each naming the file it is answered
    in. Pipe it into an agent CLI or paste it into a chat; dbprint launches no program and calls
    no model. `--check` then lists every finding on the table's annotation files.

    **Arguments:**

    - `TABLE`: one table FQN (e.g. `arboretum.seedbank.accession`), not a pattern.
    - `CONNECTION`: connection scope; resolved from `.dbprint.yaml` when omitted (the
      `auto: true` set, or the sole connection).

    **Exit codes:**

    - `0`: ok; with `--check`, no annotation finding
    - `1`: unknown table, missing manifest, a table in several connections with no
      `CONNECTION`, or `--check` found annotation findings

    **Examples:**

    - `dbprint annotate arboretum.seedbank.accession`: the brief, for reading or pasting
    - `dbprint annotate arboretum.seedbank.accession | claude -p`: hand it to an agent CLI
    - `dbprint annotate arboretum.seedbank.accession --check`: the table's annotation findings
    """

    if any(ch in table for ch in "*?["):
        click.echo("annotate takes one table FQN, not a pattern.", err=True)
        ctx.exit(EXIT_GENERIC)

    project_config = resolve_project(project)

    try:
        connections = resolve(project_config, connection)
    except ConnectionResolutionError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    resolved, exit_code, messages = resolve_connections(connections, table, select_all=False)

    if not resolved:
        for message in messages:
            click.echo(message, err=True)

        ctx.exit(exit_code)

    if len(resolved) > 1:
        names = ", ".join(conn.name for conn, _, _ in resolved)
        click.echo(f"{table} is in connections {names} - pass CONNECTION.", err=True)
        ctx.exit(EXIT_GENERIC)

    conn_config, manifest, _ = resolved[0]

    if check:
        ctx.exit(_run_check(conn_config, manifest, table))

    context = assemble_context(
        manifest,
        print_root=conn_config.print_root,
        tables=[table],
        options=AssemblyOptions(format="md", purpose="query"),
        connection_name=conn_config.name,
    ).text
    text = render_brief(
        annotate_skill_steps(),
        context,
        open_questions(manifest, conn_config.print_root, table),
        table,
        _file_prefix(conn_config.print_root, project_config.project_root),
    )

    if resolve_render_mode(tui, Console()) == "tty":
        render_human(text, Console(file=sys.stdout, force_terminal=True, soft_wrap=True))
    else:
        click.echo(text, nl=False)

    ctx.exit(EXIT_OK)


def _run_check(conn_config: ConnectionConfig, manifest: dict, table: str) -> int:
    issues = issues_for_table(validate_print(conn_config.print_root), manifest, table)
    found = [issue for issue in issues if _on_annotation_files(issue)]

    if not found:
        click.echo(f"{table}: no annotation findings")

        return EXIT_OK

    for issue in found:
        click.echo(f"  {issue.path}\n    {issue.code} ({issue.spec_ref})\n      {issue.detail}")

    return EXIT_GENERIC


def _on_annotation_files(issue: Issue) -> bool:
    file = PurePosixPath(issue.path.split("::", 1)[0]).name

    return issue.code.startswith("annotations.") or file in _ANNOTATION_FILES


def _file_prefix(print_root: Path, project_root: Path) -> str:
    try:
        return print_root.relative_to(project_root).as_posix()
    except ValueError:
        return print_root.as_posix()
