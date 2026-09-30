"""Redshift adapter package - exports the concrete RedshiftAdapter."""

from __future__ import annotations

from .adapter import RedshiftAdapter
from .connection import DIALECT


__all__ = ["DIALECT", "RedshiftAdapter"]
