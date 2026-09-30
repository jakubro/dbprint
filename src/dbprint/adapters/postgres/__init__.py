"""PostgreSQL adapter package - PostgresAdapter, its credentials bundle and its error."""

from __future__ import annotations

from .adapter import PostgresAdapter
from .connection import DIALECT, ConnectionParams, PostgresConnectionError


__all__ = [
    "DIALECT",
    "ConnectionParams",
    "PostgresAdapter",
    "PostgresConnectionError",
]
