"""ClickhouseAdapter - concrete Adapter wired to the clickhouse helper modules, built from a
credentials dict; every method asserts the session is open before delegating.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from . import ddl as ddl_module
from . import introspect as introspect_module
from . import looks_like as looks_like_module
from . import normalization as normalization_module
from . import sketch as sketch_module
from . import stats as stats_module
from .connection import ClickhouseConnectionError, Connection, ConnectionParams, CursorFactory
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


class SamplingKeyMissing(RuntimeError):
    """Raised when `SAMPLE` is requested against a table with no declared `SAMPLE BY` key.

    The driver raises `SAMPLING_NOT_SUPPORTED` for the same reason, but only after issuing the
    `CREATE TEMPORARY TABLE`; this is caught from the catalog fact `list_tables` already read.
    """


class ClickhouseAdapter(Adapter):
    """Concrete Adapter for ClickHouse, backed by clickhouse-connect's DB-API."""

    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("host", "database")
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = ("port", "user", "password")
    # SAMPLE's determinism depends on a declared SAMPLE BY key; an unmaterialized scope on a
    # table without one is not a seeded per-row guarantee.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = False

    def __init__(
        self,
        credentials: dict[str, str],
        cursor_factory: CursorFactory | None = None,
    ) -> None:
        self._params = ConnectionParams.from_credentials(credentials)
        self._connection = Connection(self._params, cursor_factory)
        # Populated by list_tables()'s own read of system.tables.sampling_key - materialize_scope
        # reads from here rather than discovering the same fact by a failed CREATE.
        self._samplable: dict[str, bool] = {}

    def connect(self) -> None:
        self._connection.open()

    def close(self) -> None:
        self._connection.close()

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        selected, samplable = introspect_module.list_tables(
            self._cursor,
            self._params.database,
            include,
            exclude,
        )
        self._samplable = samplable

        return selected

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

    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]] | None:
        return introspect_module.view_dependencies(self._cursor)

    def extract_comments(self, fqn: str) -> CommentsMeta:
        return introspect_module.comments(self._cursor, fqn)

    def estimate_row_count(self, fqn: str) -> int | None:
        return row_count_or_none(introspect_module.estimate_row_count(self._cursor, fqn))

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

    def probe_timeline(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        column: str,
        unit: Literal["day", "week", "month"],
        scope: TableScope | None = None,
    ) -> tuple[tuple[str, int], ...]:
        return stats_module.probe_timeline(self._cursor, fqn, columns, counts, column, unit, scope)

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
            self._cursor,
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
            self._cursor,
            fqn,
            columns,
            counts,
            base,
            candidates,
            scope,
        )

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        """Copy a sampled draw - `scope.sample` is always set (base.py's own contract)."""

        if not self._samplable.get(fqn, False):
            raise SamplingKeyMissing(
                f"table {fqn!r} declares no SAMPLE BY key (or is not a MergeTree-family "
                f"table), so ClickHouse's SAMPLE clause is not available on it at any "
                f"fraction.",
            )

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

    def compute_normalized_cardinality(
        self,
        fqn: str,
        column: str,
        scope: TableScope | None = None,
    ) -> int:
        return normalization_module.compute_normalized_cardinality(
            self._cursor,
            fqn,
            column,
            scope,
        )

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run user-authored SQL and return all rows; SQL assertion path (ASSERTIONS.md 3) -
        read-only is the operator's responsibility, not enforced here (ASSERTIONS.md 3.4).
        """

        cursor = self._cursor
        cursor.execute(sql)
        rows = cursor.fetchall()

        return [tuple(row) for row in rows]

    @property
    def _cursor(self) -> Any:
        if not self._connection.is_open():
            raise ClickhouseConnectionError("adapter is not connected; call connect() first")

        return self._connection.cursor
