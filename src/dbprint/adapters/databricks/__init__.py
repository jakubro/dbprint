"""Databricks adapter package - exports the concrete DatabricksAdapter."""

from __future__ import annotations

from .adapter import DatabricksAdapter
from .connection import DIALECT


__all__ = ["DIALECT", "DatabricksAdapter"]
