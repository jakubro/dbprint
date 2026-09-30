"""BigQuery adapter package - exports the concrete BigqueryAdapter."""

from __future__ import annotations

from .adapter import BigqueryAdapter
from .connection import DIALECT


__all__ = ["DIALECT", "BigqueryAdapter"]
