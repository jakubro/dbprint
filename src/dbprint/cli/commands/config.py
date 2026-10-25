"""`dbprint config` - inspect how `.dbprint.yaml` resolves; `explain` prints one table's profile.

Read-only: nothing here writes a file, and only `explain --online` opens a connection.
"""

from __future__ import annotations

from pathlib import Path

import rich_click as click
import yaml

from dbprint.config import ConnectionConfig, ProjectConfig
from dbprint.config.resolution import ConnectionResolutionError, resolve_connections
from dbprint.engine import EXIT_CONNECTION, EXIT_GENERIC, ConnectFailed
from dbprint.engine.baseline import ManifestRead, read_artifact, read_manifest, table_directory
from dbprint.engine.explain import explain
from dbprint.spec.absence import block_value
from dbprint.spec.artifacts import declared_artifacts
from ..engine_setup import ConnectionSetupError, build_engine, interruptible
from ..options import project_option, refuse_if_remote, resolve_project
from ..rendering.explain_data import render_explanation


@click.group(name="config")
def config_group() -> None:
    """Inspect `.dbprint.yaml`: how its defaults, connections and rules resolve for a table.

    Read-only. Nothing here writes a file or, without `--online`, opens a connection.
    """


@config_group.command(name="explain")
@click.argument("connection", required=False)
@click.argument("table", required=False)
@project_option
@click.option(
    "--online",
    "online",
    is_flag=True,
    default=False,
    help=(
        "Ask the database for the table's current row-count estimate instead of the count the "
        "print last recorded; size rules (`min_rows`) are evaluated against it."
    ),
)
@click.pass_context
def explain_command(
    ctx: click.Context,
    connection: str | None,
    table: str | None,
    project: str | None,
    online: bool,
) -> None:
    """Print the profile one table resolves to, each value commented with where it came from.

    Walks `defaults`, the connection and every rule in order, as `generate` does, and prints
    the result as a YAML `profile:` block whose comments name the layer that set each value -
    `built-in default`, `defaults.profile`, the connection, or a rule by its config path - with
    every rule's matcher and whether it applied. Never writes. Size rules are evaluated offline
    against the row count the print last recorded; a table the print does not hold is matched
    by name alone, which the output says.

    **Arguments:**

    - `CONNECTION`: connection to explain; resolved from `.dbprint.yaml` when omitted (the
      `auto: true` set, or the sole connection). A single argument that names no connection is
      read as `TABLE`.
    - `TABLE`: fully qualified name, e.g. `arboretum.seedbank.accession`, matched
      case-insensitively, as rules are. Omitted, the connection's own profile and its rule list
      are printed.

    **Exit codes:**

    - `0`: printed
    - `1`: the config fails to load, or the connection is unknown or ambiguous
    - `4`: `--online` could not reach the database

    **Examples:**

    - `dbprint config explain`: the connection-level profile and its rules
    - `dbprint config explain warehouse arboretum.seedbank.accession`: one table, offline
    - `dbprint config explain arboretum.seedbank.accession --online`: against the live estimate
    """

    if online:
        refuse_if_remote(project, "config explain --online")

    project_config = resolve_project(project)

    if table is None and connection is not None and connection not in project_config.connections:
        connection, table = None, connection

    conn = _one_connection(ctx, project_config, connection)
    estimate = None
    # Producers lowercase every identifier segment (SPEC 1.3), so generate only resolves this one.
    table = table.lower() if table is not None else None

    if online and table is None:
        click.echo("note: --online has no table to estimate; ignored", err=True)
    elif online and table is not None:
        try:
            with interruptible():
                setup = build_engine(conn, project_config.project_root)
                estimate = setup.engine.estimate_row_count(table)
        except (ConnectionSetupError, ConnectFailed) as exc:
            click.echo(f"error: {exc}", err=True)
            ctx.exit(EXIT_CONNECTION)

    manifest = read_manifest(conn.print_root)
    explanation = explain(
        conn,
        table,
        manifest,
        estimate=estimate,
        online=online and table is not None,
        external=_printed_external(conn.print_root, manifest, table),
    )
    click.echo(render_explanation(explanation), nl=False)


def _one_connection(
    ctx: click.Context,
    project_config: ProjectConfig,
    connection: str | None,
) -> ConnectionConfig:
    try:
        resolved = resolve_connections(project_config, connection)
    except ConnectionResolutionError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    if resolved.default is None:
        names = [c.name for c in resolved.connections]
        click.echo(
            f"several connections run by default: {names}. Pass CONNECTION to choose one.",
            err=True,
        )
        ctx.exit(EXIT_GENERIC)

    return next(c for c in resolved.connections if c.name == resolved.default)


def _printed_external(print_root: Path, manifest: ManifestRead, fqn: str | None) -> bool:
    if fqn is None:
        return False

    entry = (manifest.manifest or {}).get("tables", {}).get(fqn)
    name = declared_artifacts(entry).get("statistics") if isinstance(entry, dict) else None

    if not isinstance(entry, dict) or not isinstance(name, str):
        return False

    try:
        statistics = read_artifact(table_directory(print_root, fqn, entry) / name)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False

    return isinstance(statistics, dict) and block_value(statistics, "external") is True
