"""ClickHouse adapter package - exports the concrete ClickhouseAdapter."""

from __future__ import annotations

from .adapter import ClickhouseAdapter
from ..dialect import Dialect


# clickhouse-connect's DB-API defaults to pyformat (%s); the adapter does not override it.
DIALECT = Dialect(vendor="clickhouse", paramstyle="pyformat")

__all__ = ["DIALECT", "ClickhouseAdapter"]
