"""Redshift adapter package - exports the concrete RedshiftAdapter."""

from __future__ import annotations

from .adapter import RedshiftAdapter
from ..dialect import Dialect


# redshift_connector defaults to pyformat, same as psycopg2; the adapter does not override it.
DIALECT = Dialect(vendor="redshift", paramstyle="pyformat")

__all__ = ["DIALECT", "RedshiftAdapter"]
