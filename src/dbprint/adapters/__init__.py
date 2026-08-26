"""Database adapters - Adapter ABC, intermediate types, and the mock adapter."""

from __future__ import annotations

from .base import (
    Adapter,
    AdapterType,
    BaseStats,
    ColumnMeta,
    ColumnStats,
    CommentsMeta,
    FkAction,
    ForeignKeyMeta,
    Frequencies,
    Freshness,
    Grain,
    GrainDetection,
    GrainKey,
    IndexMeta,
    Inferred,
    NullPattern,
    NullPatterns,
    PhysicalLayout,
    PhysicalLayoutKey,
    Range,
    StatisticsConfig,
    TableCounts,
    TableMeta,
    TableScope,
    TableType,
    UniqueKeyMeta,
    ValueCount,
)
from .mock import MockAdapter, MockTable
from .mysql import MysqlAdapter
from .postgres import PostgresAdapter
from .snowflake import SnowflakeAdapter


__all__ = [
    "Adapter",
    "AdapterType",
    "BaseStats",
    "ColumnMeta",
    "ColumnStats",
    "CommentsMeta",
    "FkAction",
    "ForeignKeyMeta",
    "Frequencies",
    "Freshness",
    "Grain",
    "GrainDetection",
    "GrainKey",
    "IndexMeta",
    "Inferred",
    "MockAdapter",
    "MockTable",
    "MysqlAdapter",
    "NullPattern",
    "NullPatterns",
    "PhysicalLayout",
    "PhysicalLayoutKey",
    "PostgresAdapter",
    "Range",
    "SnowflakeAdapter",
    "StatisticsConfig",
    "TableCounts",
    "TableMeta",
    "TableScope",
    "TableType",
    "UniqueKeyMeta",
    "ValueCount",
]
