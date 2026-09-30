"""duckdb adapter package - DuckdbAdapter, its credentials bundle and its error."""

from __future__ import annotations

from .adapter import DuckdbAdapter
from .connection import DIALECT, ConnectionParams, DuckdbConnectionError


__all__ = [
    "DIALECT",
    "ConnectionParams",
    "DuckdbAdapter",
    "DuckdbConnectionError",
]
