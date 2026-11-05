"""SQLite adapter package - SqliteAdapter, its credentials bundle and its error."""

from __future__ import annotations

from .adapter import SqliteAdapter
from .connection import DIALECT, ConnectionParams, SqliteConnectionError


__all__ = [
    "DIALECT",
    "ConnectionParams",
    "SqliteAdapter",
    "SqliteConnectionError",
]
