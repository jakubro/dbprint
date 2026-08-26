"""`dbprint serve` - launch the MCP server over stdio or HTTP.

Gates on the `[mcp]` install extra at invocation time, exiting 1 with the documented hint
(MCP.md 1) when missing. The served connection set resolves by the CLI's usual rules, rooted
at `--project-dir` when given rather than at the working directory.
"""

from __future__ import annotations

from pathlib import Path

import rich_click as click

from dbprint.config import load_project
from dbprint.engine import EXIT_GENERIC
from ..resolution import ConnectionResolutionError
from ..resolution import resolve as resolve_connections


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@click.command(name="serve")
@click.argument("conn", required=False)
@click.option(
    "--project-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directory to resolve .dbprint.yaml from, for a client that starts the server "
    "outside the project. Defaults to the working directory.",
)
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"], case_sensitive=False),
    default="stdio",
    show_default=True,
    help="Wire transport. stdio for editor/agent integration; http for local sockets.",
)
@click.option(
    "--host",
    type=str,
    default="127.0.0.1",
    show_default=True,
    help="HTTP transport bind address. Must be loopback (127.0.0.1, ::1, or localhost).",
)
@click.option(
    "--port",
    type=int,
    default=None,
    help="HTTP transport TCP port. Required when --transport http.",
)
@click.option(
    "--read-only/--no-read-only",
    default=True,
    show_default=True,
    help="Read-only over committed prints; no other mode is supported.",
)
@click.pass_context
def serve_command(
    ctx: click.Context,
    conn: str | None,
    project_dir: Path | None,
    transport: str,
    host: str,
    port: int | None,
    read_only: bool,
) -> None:
    """Run a read-only MCP server over the committed prints.

    Exposes the committed prints as Model Context Protocol resources and tools
    for editor and agent integration. Requires the `[mcp]` extra
    (`pip install dbprint[mcp]`); exits 1 with an install hint when it is
    missing. Serves the resolved connection set read-only - no database
    connection. stdio transport by default; HTTP binds to loopback only.
    The project resolves from the working directory unless `--project-dir`
    names another.

    **Arguments:**

    - `CONN`: connection(s) to serve; resolved from `.dbprint.yaml` when
      omitted (the `auto: true` set, or the sole connection).

    **Exit codes:**

    - `0`: clean shutdown
    - `1`: missing `[mcp]` extra, bad transport args, or unresolved connection

    **Examples:**

    - `dbprint serve`: stdio (editor / agent)
    - `dbprint serve --transport http --port 8765`: loopback HTTP server
    - `dbprint serve --project-dir /srv/analytics`: a project outside the working directory
    """

    if not read_only:
        click.echo("Only --read-only is supported.", err=True)
        ctx.exit(EXIT_GENERIC)

    try:
        from dbprint import mcp as mcp_pkg
    except ImportError:
        click.echo(
            "Install dbprint[mcp] to use the serve command.",
            err=True,
        )
        ctx.exit(EXIT_GENERIC)

    transport_lower = transport.lower()

    if transport_lower == "http":
        if port is None:
            click.echo("--port is required when --transport http.", err=True)
            ctx.exit(EXIT_GENERIC)

        if host not in _LOOPBACK_HOSTS:
            click.echo(
                f"--host must be loopback ({sorted(_LOOPBACK_HOSTS)}); got {host!r}.",
                err=True,
            )
            ctx.exit(EXIT_GENERIC)

    project_config = load_project(start=project_dir)

    try:
        connections = resolve_connections(project_config, conn)
    except ConnectionResolutionError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    # Only the resolved connections, so MCP state defaulting matches CLI resolution.
    served_names = {c.name for c in connections}
    served_config = type(project_config)(
        project_root=project_config.project_root,
        connections={
            name: cfg for name, cfg in project_config.connections.items() if name in served_names
        },
    )
    state = mcp_pkg.build_state(
        served_config,
        conn,
        configured=frozenset(project_config.connections),
    )

    if transport_lower == "stdio":
        mcp_pkg.serve_stdio(state)
    else:
        assert port is not None
        mcp_pkg.serve_http(state, host, port)
