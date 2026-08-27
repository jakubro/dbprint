"""`dbprint context` - assembles agent-ready fragments from committed prints."""

from __future__ import annotations

import fnmatch
from difflib import get_close_matches
from pathlib import Path
from typing import Any

import rich_click as click
import yaml

from dbprint.config import ConnectionConfig
from dbprint.engine import EXIT_GENERIC, EXIT_OK, AssemblyOptions, assemble_context
from dbprint.engine.baseline import manifest_shape_error
from ..options import project_option, resolve_project
from ..resolution import ConnectionResolutionError, resolve


@click.command(name="context")
@click.argument("target", required=False)
@click.argument("conn", required=False)
@project_option
@click.option(
    "--all",
    "select_all",
    is_flag=True,
    default=False,
    help="Render every table in the manifest.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["md", "json", "yaml"], case_sensitive=False),
    default="md",
    show_default=True,
    help="Output format. json and yaml omit each column's sketch payload; the table's "
    "own statistics.yaml carries it.",
)
@click.option("--no-ddl", is_flag=True, default=False, help="Omit the DDL section.")
@click.option(
    "--no-relationships",
    is_flag=True,
    default=False,
    help="Omit the Relationships section.",
)
@click.option("--no-description", is_flag=True, default=False, help="Omit the Description section.")
@click.option(
    "--no-annotations",
    is_flag=True,
    default=False,
    help="Omit the Annotations section.",
)
@click.option("--no-stats", is_flag=True, default=False, help="Omit the Cardinality table.")
@click.option(
    "--budget",
    "budget",
    type=int,
    default=None,
    help="Soft output cap in tokens (approx chars/4); stop at the first section that "
    "would overflow. e.g. 4000",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write output to FILE instead of stdout.",
)
@click.pass_context
def context_command(
    ctx: click.Context,
    target: str | None,
    conn: str | None,
    project: str | None,
    select_all: bool,
    fmt: str,
    no_ddl: bool,
    no_relationships: bool,
    no_description: bool,
    no_annotations: bool,
    no_stats: bool,
    budget: int | None,
    output_path: Path | None,
) -> None:
    """Emit an agent-ready context fragment for committed tables.

    Assembles per-table artifacts (DDL, statistics, relationships, description,
    annotations) into a single prompt-ready block and writes it to stdout or
    `--output`.
    Offline - reads only committed prints. Select one table by FQN, a set by
    fnmatch pattern, or every table with `--all`. Markdown by default;
    `--budget` caps the output and stops at the first section that would
    overflow.

    **Arguments:**

    - `TARGET`: table FQN (e.g. `arboretum.seedbank.accession`), an fnmatch pattern
      (e.g. `public.*`), or omit and pass `--all` for every table.
    - `CONN`: connection scope; resolved from `.dbprint.yaml` when omitted (the
      `auto: true` set, or the sole connection).

    **Exit codes:**

    - `0`: ok
    - `1`: no match, missing manifest, or budget too small

    **Examples:**

    - `dbprint context arboretum.seedbank.accession`: one table, full Markdown
    - `dbprint context 'public.*'`: every public table (pattern)
    - `dbprint context --all --no-ddl`: every table, skip DDL
    - `dbprint context users --budget 4000`: cap output near 4000 tokens
    """

    if select_all and target is not None:
        click.echo("Pass either TABLE/PATTERN or --all, not both.", err=True)
        ctx.exit(EXIT_GENERIC)

    if not select_all and target is None:
        click.echo(
            "Specify a TABLE FQN, a PATTERN, or --all. See `dbprint context --help`.",
            err=True,
        )
        ctx.exit(EXIT_GENERIC)

    project_config = resolve_project(project)

    try:
        connections = resolve(project_config, conn)
    except ConnectionResolutionError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    options = AssemblyOptions(
        format=fmt.lower(),
        include_ddl=not no_ddl,
        include_description=not no_description,
        include_annotations=not no_annotations,
        include_stats=not no_stats,
        include_relationships=not no_relationships,
        budget=budget,
    )

    rendered_chunks: list[str] = []
    overall_exit = EXIT_OK

    for conn_config in connections:
        manifest = _load_manifest(conn_config)

        if manifest is None:
            click.echo(f"{conn_config.name}: {_unusable_manifest_cause(conn_config)}", err=True)
            overall_exit = max(overall_exit, EXIT_GENERIC)
            continue

        try:
            resolved_tables = _resolve_tables(manifest, target, select_all)
        except _NoMatch as exc:
            click.echo(str(exc), err=True)
            overall_exit = max(overall_exit, EXIT_GENERIC)
            continue

        result = assemble_context(
            manifest,
            print_root=_print_root(conn_config),
            tables=resolved_tables,
            options=options,
            connection_name=conn_config.name,
        )

        if result.tables_included == 0:
            click.echo(f"{conn_config.name}: budget too small to include any table.", err=True)
            overall_exit = max(overall_exit, EXIT_GENERIC)
            continue

        rendered_chunks.append(result.text)

    text = (
        ("\n\n---\n\n".join(c.rstrip() for c in rendered_chunks if c.strip()))
        if rendered_chunks
        else ""
    )

    if text and not text.endswith("\n"):
        text += "\n"

    if output_path is not None:
        output_path.write_text(text)
    else:
        click.echo(text, nl=False)

    ctx.exit(overall_exit)


class _NoMatch(ValueError):
    """Raised when the selection matches no manifest table."""


def _resolve_tables(manifest: dict[str, Any], target: str | None, select_all: bool) -> list[str]:
    """Pick the ordered list of table FQNs per the selection rules."""

    tables = list((manifest.get("tables") or {}).keys())

    if select_all:
        return sorted(tables)

    assert target is not None

    if any(ch in target for ch in "*?["):
        matched = sorted(t for t in tables if fnmatch.fnmatchcase(t, target))

        if not matched:
            hint_list = ", ".join(sorted(tables)) or "(none)"

            raise _NoMatch(f"pattern {target!r} matched no tables. Configured tables: {hint_list}")

        return matched

    if target in tables:
        return [target]

    suggestions = get_close_matches(target, tables, n=1, cutoff=0.6)
    hint = f" Did you mean: {suggestions[0]}?" if suggestions else ""

    raise _NoMatch(f"no table {target!r} found in manifest.{hint}")


def _load_manifest(conn: ConnectionConfig) -> dict[str, Any] | None:
    path = _manifest_path(conn)

    if not path.is_file():
        return None

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return None

    if manifest_shape_error(data) is not None:
        return None

    return data if isinstance(data, dict) else None


def _unusable_manifest_cause(conn: ConnectionConfig) -> str:
    """Why the manifest could not be read, in the words the user needs.

    Missing and wrong-shape stay apart: conflating them sends the user to `generate`, which
    overwrites the print instead of fixing the file. Re-reads, since the loader has failed.
    """

    path = _manifest_path(conn)

    if not path.is_file():
        return f"no manifest at {path}. Run `dbprint generate {conn.name}` first."

    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        return f"could not parse {path}: {exc}"

    return f"ignoring {path}: {manifest_shape_error(data) or 'unusable manifest'}"


def _manifest_path(conn: ConnectionConfig) -> Path:
    return _print_root(conn) / "manifest.yaml"


def _print_root(conn: ConnectionConfig) -> Path:
    return conn.output / conn.name
