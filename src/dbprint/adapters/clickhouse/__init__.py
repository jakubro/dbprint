"""ClickHouse adapter package - exports the concrete ClickhouseAdapter."""

from __future__ import annotations

from .adapter import ClickhouseAdapter
from .connection import DIALECT


__all__ = ["DIALECT", "ClickhouseAdapter"]
