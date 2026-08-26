"""MysqlAdapter - concrete Adapter wired to the mysql helper modules.

Constructed from a credentials dict the engine fills from `REQUIRED_KEYS`; methods assert the
session is open first. The wire protocol is served by MariaDB (test substrate) and Oracle MySQL.
"""

from __future__ import annotations

from typing import Any, ClassVar

from . import ddl as ddl_module
from . import introspect as introspect_module
from . import looks_like as looks_like_module
from . import sketch as sketch_module
from . import stats as stats_module
from .connection import Connection, ConnectionParams, MysqlConnectionError, exec_query
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


class MysqlAdapter(Adapter):
    """Concrete Adapter for MySQL / MariaDB backed by mysql-connector-python."""

    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("host", "port", "database", "user", "password")

    def __init__(self, credentials: dict[str, str]) -> None:
        self._params = ConnectionParams.from_credentials(credentials)
        self._connection = Connection(self._params)

    def connect(self) -> None:
        self._connection.open()

    def close(self) -> None:
        self._connection.close()

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        return introspect_module.list_tables(self._cursor, include, exclude)

    def extract_ddl(self, fqn: str) -> str:
        return ddl_module.extract_ddl(self._cursor, fqn)

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        return introspect_module.columns(self._cursor, fqn)

    def default_collation(self) -> str:
        return introspect_module.default_collation(self._cursor)

    def introspect_relationships(self, fqn: str) -> list[ForeignKeyMeta]:
        return introspect_module.relationships(self._cursor, fqn)

    def introspect_indexes(self, fqn: str) -> list[IndexMeta]:
        return introspect_module.indexes(self._cursor, fqn)

    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        return introspect_module.unique_keys(self._cursor, fqn)

    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        return introspect_module.physical_layout(self._cursor, fqn)

    def extract_comments(self, fqn: str) -> CommentsMeta:
        return introspect_module.comments(self._cursor, fqn)

    def estimate_row_count(self, fqn: str) -> int | None:
        return row_count_or_none(introspect_module.table_rows_estimate(self._cursor, fqn))

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, dict[str, BaseStats]]:
        del config

        return stats_module.compute_base(self._cursor, fqn, columns, scope)

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
            self._cursor,
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
            self._cursor,
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
        return stats_module.probe_grain(self._cursor, fqn, columns, counts, candidates, scope)

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
            self._cursor,
            fqn,
            columns,
            counts,
            base,
            candidates,
            scope,
        )

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        return stats_module.materialize(self._cursor, fqn, scope)

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        del fqn

        stats_module.release(self._cursor, scope)

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
    ) -> list[Any]:
        return looks_like_module.sample_distinct(self._cursor, fqn, column, n, scope)

    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: SketchKind,
        k: int,
    ) -> tuple[int, ...]:
        return sketch_module.compute_key_sketch(self._cursor, fqn, column, sql_type, kind, k)

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run user-authored SQL and return all rows; SQL assertion path (ASSERTIONS.md 3).

        Read-only is the operator's responsibility, not enforced here (ASSERTIONS.md 3.4).
        """

        cursor = exec_query(self._cursor, sql)
        rows = cursor.fetchall()

        return [tuple(row) for row in rows]

    @property
    def _cursor(self) -> Any:
        if not self._connection.is_open():
            raise MysqlConnectionError("adapter is not connected; call connect() first")

        return self._connection.cursor
