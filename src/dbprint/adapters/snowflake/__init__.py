"""Snowflake adapter package public surface."""

from __future__ import annotations

from .adapter import SnowflakeAdapter
from .connection import ConnectionParams, Cursor, CursorFactory, SnowflakeConnectionError
from .introspect import IdentifierRejected
from ..dialect import Dialect


# Connector opens with paramstyle="qmark" (connection.py); statements bind via `?`.
DIALECT = Dialect(vendor="snowflake", paramstyle="qmark")

__all__ = [
    "DIALECT",
    "ConnectionParams",
    "Cursor",
    "CursorFactory",
    "IdentifierRejected",
    "SnowflakeAdapter",
    "SnowflakeConnectionError",
]
