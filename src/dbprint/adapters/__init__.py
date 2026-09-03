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
    Length,
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
from .bigquery import BigqueryAdapter
from .clickhouse import ClickhouseAdapter
from .databricks import DatabricksAdapter
from .duckdb import DuckdbAdapter
from .mock import MockAdapter, MockTable
from .mysql import MysqlAdapter
from .postgres import PostgresAdapter
from .redshift import RedshiftAdapter
from .snowflake import SnowflakeAdapter


__all__ = [
    "Adapter",
    "AdapterType",
    "BaseStats",
    "BigqueryAdapter",
    "ClickhouseAdapter",
    "ColumnMeta",
    "ColumnStats",
    "CommentsMeta",
    "DatabricksAdapter",
    "DuckdbAdapter",
    "FkAction",
    "ForeignKeyMeta",
    "Frequencies",
    "Freshness",
    "Grain",
    "GrainDetection",
    "GrainKey",
    "IndexMeta",
    "Inferred",
    "Length",
    "MockAdapter",
    "MockTable",
    "MysqlAdapter",
    "NullPattern",
    "NullPatterns",
    "PhysicalLayout",
    "PhysicalLayoutKey",
    "PostgresAdapter",
    "Range",
    "RedshiftAdapter",
    "SnowflakeAdapter",
    "StatisticsConfig",
    "TableCounts",
    "TableMeta",
    "TableScope",
    "TableType",
    "UniqueKeyMeta",
    "ValueCount",
]
