"""Browsable docs site for a committed print: `dbprint docs serve` / `dbprint docs build`.

Gated behind the `[docs]` extra: importing this package requires `flask` and `markdown`,
and the CLI turns the resulting `ImportError` into an install hint.
"""

from __future__ import annotations

from dbprint.config import ConnectionConfig
from .build import BuildResult, OutputNotOwnedError, build_site
from .web import create_app


__all__ = ["BuildResult", "OutputNotOwnedError", "build_site", "create_app", "serve"]


def serve(connections: list[ConnectionConfig], host: str, port: int) -> None:
    """Run the docs app for `connections`, blocking until interrupted."""

    create_app(connections).run(host=host, port=port)
