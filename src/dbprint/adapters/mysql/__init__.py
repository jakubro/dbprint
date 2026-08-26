"""MySQL adapter package - exports the concrete MysqlAdapter."""

from __future__ import annotations

from .adapter import MysqlAdapter
from ..dialect import Dialect


# mysql-connector-python defaults to pyformat; the adapter does not override it.
DIALECT = Dialect(vendor="mysql", paramstyle="pyformat")

__all__ = ["DIALECT", "MysqlAdapter"]
