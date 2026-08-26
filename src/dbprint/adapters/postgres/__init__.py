"""PostgreSQL adapter package - PostgresAdapter, its credentials bundle and its error."""

from __future__ import annotations

from .adapter import PostgresAdapter
from .connection import ConnectionParams, PostgresConnectionError
from ..dialect import Dialect


# psycopg3 defaults to pyformat and the adapter does not override it.
DIALECT = Dialect(vendor="postgres", paramstyle="pyformat")

__all__ = [
    "DIALECT",
    "ConnectionParams",
    "PostgresAdapter",
    "PostgresConnectionError",
]
