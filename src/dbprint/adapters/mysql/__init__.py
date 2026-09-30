"""MySQL adapter package - exports the concrete MysqlAdapter."""

from __future__ import annotations

from .adapter import MysqlAdapter
from .connection import DIALECT


__all__ = ["DIALECT", "MysqlAdapter"]
