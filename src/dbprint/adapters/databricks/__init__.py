"""Databricks adapter package - exports the concrete DatabricksAdapter."""

from __future__ import annotations

from .adapter import DatabricksAdapter
from ..dialect import Dialect


# databricks-sql-connector defaults to native parameter binding (use_inline_params=False), which
# the adapter does not override, and native positional binding takes `?` markers.
DIALECT = Dialect(vendor="databricks", paramstyle="qmark")

__all__ = ["DIALECT", "DatabricksAdapter"]
