"""`dbprint docs` - browsable HTML view of a print: `serve` (live) and `build` (static).

Gates on the `[docs]` extra at invocation time, exiting 1 with an install hint when missing.
`CONN` omitted resolves as every other command does, with an explicit `--all` to widen.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import rich_click as click

from dbprint.config import ConnectionConfig
from dbprint.engine import EXIT_GENERIC, EXIT_OK
from ..options import keep_fresh, project_option, resolve_project
from ..resolution import ConnectionResolutionError
from ..resolution import resolve as resolve_connections


_DEFAULT_PORT = 8765
_DEFAULT_OUTPUT = Path("dbprint-docs")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@click.group(name="docs")
def docs_group() -> None:
    """Browse a committed print as an HTML site: `serve` it live, or `build` it static.

    Renders the whole print - column statistics, relationships, DDL, human annotations - as
    pages a reader clicks through, rather than opening `statistics.yaml` by hand. Requires the
    `[docs]` extra (`pip install dbprint[docs]`); both subcommands exit 1 with an install hint
    when it is missing.
    """


@docs_group.command(name="serve")
@click.argument("conn", required=False)
@project_option
@click.option("--all", "select_all", is_flag=True, default=False, help="Serve every connection.")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address. Must be loopback (127.0.0.1, ::1, or localhost).",
)
@click.option(
    "--port",
    type=int,
    default=_DEFAULT_PORT,
    show_default=True,
    help="TCP port to bind.",
)
@click.pass_context
def serve_command(
    ctx: click.Context,
    conn: str | None,
    project: str | None,
    select_all: bool,
    host: str,
    port: int,
) -> None:
    """Serve the docs site live over HTTP, re-reading the print on every request.

    Binds loopback only. Re-reads every artifact from disk on each request, so a page
    reflects the latest `generate` without restarting the server.

    **Arguments:**

    - `CONN`: connection(s) to serve; resolved from `.dbprint.yaml` when omitted (the
      `auto: true` set, or the sole connection). Pass `--all` for completeness instead.

    **Exit codes:**

    - `0`: clean shutdown
    - `1`: missing `[docs]` extra, a non-loopback `--host`, or an unresolved connection

    **Examples:**

    - `dbprint docs serve`: the resolved connection set on `127.0.0.1:8765`
    - `dbprint docs serve --all --port 9000`: every connection, custom port
    """

    if host not in _LOOPBACK_HOSTS:
        click.echo(f"--host must be loopback ({sorted(_LOOPBACK_HOSTS)}); got {host!r}.", err=True)
        ctx.exit(EXIT_GENERIC)

    docs_pkg = _import_docs(ctx)
    connections = _resolve(ctx, conn, project, select_all)
    keep_fresh(project)

    docs_pkg.serve(connections, host, port)


@docs_group.command(name="build")
@click.argument("conn", required=False)
@project_option
@click.option("--all", "select_all", is_flag=True, default=False, help="Build every connection.")
@click.option(
    "--output",
    "output_path",
    type=click.Path(file_okay=False, path_type=Path),
    default=_DEFAULT_OUTPUT,
    show_default=True,
    help="Output directory. Recreated from scratch each run.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Recreate --output even if it exists without a prior build's marker.",
)
@click.pass_context
def build_command(
    ctx: click.Context,
    conn: str | None,
    project: str | None,
    select_all: bool,
    output_path: Path,
    force: bool,
) -> None:
    """Write the docs site as static files - servable by any host that resolves `path/index.html`.

    Recreates `--output` from scratch on every run, so a page for a table the print no longer
    has never lingers. Refuses to recreate a directory it did not itself create, unless
    `--force` is passed.

    **Arguments:**

    - `CONN`: connection(s) to build; resolved from `.dbprint.yaml` when omitted (the
      `auto: true` set, or the sole connection). Pass `--all` for completeness instead.

    **Exit codes:**

    - `0`: ok
    - `1`: missing `[docs]` extra, an unresolved connection, `--output` exists without this
      tool's marker and `--force` was not passed, or at least one route returned non-200 and
      was not written

    **Examples:**

    - `dbprint docs build`: the resolved connection set to `./dbprint-docs/`
    - `dbprint docs build --all --output /tmp/site`: every connection to a chosen path
    """

    docs_pkg = _import_docs(ctx)
    connections = _resolve(ctx, conn, project, select_all)

    try:
        result = docs_pkg.build_site(connections, output_path, force=force)
    except docs_pkg.OutputNotOwnedError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    click.echo(f"Wrote {result.pages_written} pages to {result.output}")

    if result.failed_routes:
        for route in result.failed_routes:
            click.echo(f"  failed: {route}", err=True)

        ctx.exit(EXIT_GENERIC)

    ctx.exit(EXIT_OK)


def _import_docs(ctx: click.Context) -> ModuleType:
    """Lazily import `dbprint.docs`, exiting with an install hint when the extra is missing."""

    try:
        from dbprint import docs as docs_pkg
    except ImportError:
        click.echo("Install dbprint[docs] to use the docs command.", err=True)
        ctx.exit(EXIT_GENERIC)

    return docs_pkg


def _resolve(
    ctx: click.Context,
    conn: str | None,
    project: str | None,
    select_all: bool,
) -> list[ConnectionConfig]:
    """Every connection to render: `--all` widens past `resolve()`'s auto-set/single default."""

    if select_all and conn is not None:
        click.echo("Pass either CONN or --all, not both.", err=True)
        ctx.exit(EXIT_GENERIC)

    project_config = resolve_project(project)

    if select_all:
        return list(project_config.connections.values())

    try:
        return resolve_connections(project_config, conn)
    except ConnectionResolutionError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)
