"""duckdb adapter package - DuckdbAdapter, its credentials bundle and its error."""

from __future__ import annotations

from .adapter import DuckdbAdapter
from .connection import ConnectionParams, DuckdbConnectionError
from ..dialect import Dialect


# The Python duckdb driver binds parameters positionally with `?`, like sqlite3.
DIALECT = Dialect(vendor="duckdb", paramstyle="qmark")

__all__ = [
    "DIALECT",
    "ConnectionParams",
    "DuckdbAdapter",
    "DuckdbConnectionError",
]
