"""PostgresAdapter - concrete Adapter wired to the postgres helper modules.

Constructed from a credentials dict the engine fills from `REQUIRED_KEYS`; every method
asserts the connection is open before delegating to a helper module.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal, LiteralString, cast

from . import ddl as ddl_module
from . import introspect as introspect_module
from . import looks_like as looks_like_module
from . import normalization as normalization_module
from . import sketch as sketch_module
from . import stats as stats_module
from .connection import Connection, ConnectionParams, PostgresConnectionError, exec_query
from ..base import (
    Adapter,
    BaseStats,
    ColumnMeta,
    ColumnProgress,
    ColumnStats,
    CommentsMeta,
    ForeignKeyMeta,
    IndexMeta,
    NullPatterns,
    PhysicalLayout,
    SketchKind,
    StatisticsConfig,
    TableCounts,
    TableMeta,
    TableScope,
    UniqueKeyMeta,
    row_count_or_none,
)


class PostgresAdapter(Adapter):
    """Concrete Adapter for PostgreSQL backed by psycopg3 + pg_dump."""

    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("host", "port", "database", "user", "password")
    # BERNOULLI decides membership per row by hashing (block, offset, seed) - an
    # unmaterialized `sample` scope still reads the same rows on every statement.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = True

    def __init__(self, credentials: dict[str, str]) -> None:
        self._params = ConnectionParams.from_credentials(credentials)
        self._connection = Connection(self._params)

    def connect(self) -> None:
        self._connection.open()

    def close(self) -> None:
        self._connection.close()

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        return introspect_module.list_tables(self._psycopg, include, exclude)

    def extract_ddl(self, fqn: str) -> str:
        self._require_open()

        return ddl_module.extract_ddl(self._params, fqn)

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        return introspect_module.columns(self._psycopg, fqn)

    def default_collation(self) -> str:
        return introspect_module.default_collation(self._psycopg)

    def introspect_relationships(self, fqn: str) -> list[ForeignKeyMeta]:
        return introspect_module.relationships(self._psycopg, fqn)

    def introspect_indexes(self, fqn: str) -> list[IndexMeta]:
        return introspect_module.indexes(self._psycopg, fqn)

    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        return introspect_module.unique_keys(self._psycopg, fqn)

    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        return introspect_module.physical_layout(self._psycopg, fqn)

    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]] | None:
        return introspect_module.view_dependencies(self._psycopg)

    def extract_comments(self, fqn: str) -> CommentsMeta:
        return introspect_module.comments(self._psycopg, fqn)

    def estimate_row_count(self, fqn: str) -> int | None:
        return row_count_or_none(introspect_module.reltuples_estimate(self._psycopg, fqn))

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, dict[str, BaseStats]]:
        del config

        return stats_module.compute_base(self._psycopg, fqn, columns, scope)

    def compute_column_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        fk_source_columns: frozenset[str],
        *,
        suppress_values: frozenset[str] = frozenset(),
        on_column: ColumnProgress | None = None,
        scope: TableScope | None = None,
    ) -> dict[str, ColumnStats]:
        return stats_module.compute_columns(
            self._psycopg,
            fqn,
            columns,
            config,
            counts,
            base,
            fk_source_columns,
            suppress_values,
            on_column,
            scope,
        )

    def compute_null_patterns(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        scope: TableScope | None = None,
    ) -> NullPatterns | None:
        return stats_module.compute_null_patterns(
            self._psycopg,
            fqn,
            columns,
            config,
            counts,
            base,
            scope,
        )

    def probe_grain(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        candidates: tuple[tuple[str, str], ...],
        scope: TableScope | None = None,
    ) -> tuple[tuple[str, str], ...]:
        return stats_module.probe_grain(self._psycopg, fqn, columns, counts, candidates, scope)

    def probe_timeline(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        column: str,
        unit: Literal["day", "week", "month"],
        scope: TableScope | None = None,
    ) -> tuple[tuple[str, int], ...]:
        return stats_module.probe_timeline(self._psycopg, fqn, columns, counts, column, unit, scope)

    def compute_populated_windows(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        anchor_column: str,
        subject_columns: tuple[str, ...],
        scope: TableScope | None = None,
    ) -> dict[str, tuple[str, str]]:
        return stats_module.compute_populated_windows(
            self._psycopg,
            fqn,
            columns,
            counts,
            anchor_column,
            subject_columns,
            scope,
        )

    def probe_dependencies(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        base: dict[str, BaseStats],
        candidates: tuple[tuple[str, str], ...],
        scope: TableScope | None = None,
    ) -> dict[tuple[str, str], float]:
        return stats_module.probe_dependencies(
            self._psycopg,
            fqn,
            columns,
            counts,
            base,
            candidates,
            scope,
        )

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        return stats_module.materialize(self._psycopg, fqn, scope)

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        del fqn

        stats_module.release(self._psycopg, scope)

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
    ) -> list[Any]:
        return looks_like_module.sample_distinct(self._psycopg, fqn, column, n, scope)

    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: SketchKind,
        k: int,
    ) -> tuple[int, ...]:
        return sketch_module.compute_key_sketch(self._psycopg, fqn, column, sql_type, kind, k)

    def compute_normalized_cardinality(
        self,
        fqn: str,
        column: str,
        scope: TableScope | None = None,
    ) -> int:
        return normalization_module.compute_normalized_cardinality(
            self._psycopg,
            fqn,
            column,
            scope,
        )

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run user-authored SQL and return all rows; SQL assertion path (ASSERTIONS.md 3).

        A fresh read-only transaction per call, so accidental DDL/DML fails fast.
        """

        self._require_open()
        conn = self._psycopg

        # autocommit is on at the connection level, so wrap user SQL explicitly.
        with conn.transaction():
            conn.execute(cast(LiteralString, "SET TRANSACTION READ ONLY"))
            cursor = exec_query(conn, sql)
            rows = cursor.fetchall()

        return [tuple(row) for row in rows]

    # Helpers

    @property
    def _psycopg(self):
        self._require_open()

        return self._connection.psycopg_connection

    def _require_open(self) -> None:
        if not self._connection.is_open():
            raise PostgresConnectionError("adapter is not connected; call connect() first")
