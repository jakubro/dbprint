"""BigQuery adapter package - exports the concrete BigqueryAdapter."""

from __future__ import annotations

from .adapter import BigqueryAdapter
from ..dialect import Dialect


DIALECT = Dialect(vendor="bigquery", paramstyle="pyformat")

__all__ = ["DIALECT", "BigqueryAdapter"]
