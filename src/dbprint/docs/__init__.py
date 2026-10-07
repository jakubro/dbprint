"""Browsable docs site for a committed print: `dbprint docs serve` / `dbprint docs build`.

Gated behind the `[docs]` extra: importing this package requires `flask` and `markdown`, and
`serve` requires `waitress`; the CLI turns the resulting `ImportError` into an install hint.
"""

from __future__ import annotations

from collections.abc import Callable

from dbprint.config import ConnectionConfig
from .build import BuildResult, OutputNotOwnedError, build_site
from .web import create_app


__all__ = ["BuildResult", "OutputNotOwnedError", "build_site", "create_app", "serve"]


def serve(
    connections: list[ConnectionConfig],
    host: str,
    port: int,
    on_listening: Callable[[], None],
) -> None:
    """Run the docs app for `connections` under waitress, blocking until interrupted.

    Binds before `on_listening` runs, so a port in use raises `OSError` and announces nothing.
    """

    from waitress import create_server

    server = create_server(create_app(connections), host=host, port=port)
    on_listening()
    server.run()
