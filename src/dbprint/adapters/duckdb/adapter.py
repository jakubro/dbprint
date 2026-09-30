"""DuckdbAdapter - concrete Adapter wired to the duckdb helper modules, built from a
credentials dict; every method asserts the connection is open before delegating.
"""

from __future__ import annotations

import copy
from typing import Any, ClassVar, Literal, Self

from . import ddl as ddl_module
from . import introspect as introspect_module
from . import looks_like as looks_like_module
from . import normalization as normalization_module
from . import sketch as sketch_module
from . import stats as stats_module
from .connection import (
    DIALECT,
    Connection,
    ConnectionParams,
    CursorFactory,
    DuckdbConnectionError,
    exec_query,
)
from ..base import (
    Adapter,
    BaseStats,
    ColumnMeta,
    ColumnProgress,
    CommentsMeta,
    ForeignKeyMeta,
    IndexMeta,
    NullPatterns,
    PhaseA,
    PhaseB,
    PhysicalLayout,
    SketchKind,
    StatisticsConfig,
    TableCounts,
    TableMeta,
    TableScope,
    UniqueKeyMeta,
    row_count_or_none,
)
from ..identifiers import Identity, IdentityRegistry


class DuckdbAdapter(Adapter):
    """Concrete Adapter for duckdb, in-process - no server, no separate driver process."""

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("database",)
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = ("read_only",)
    PATH_KEYS: ClassVar[tuple[str, ...]] = ("database",)
    # Same BERNOULLI guarantee as Postgres - an unmaterialized `sample` scope stays coherent.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = True

    def __init__(
        self,
        credentials: dict[str, str],
        cursor_factory: CursorFactory | None = None,
        *,
        statement_timeout: int | None = None,
    ) -> None:
        self._params = ConnectionParams.from_credentials(
            credentials,
            statement_timeout=statement_timeout,
        )
        self._connection = Connection(self._params, cursor_factory)
        self._identities = IdentityRegistry(DIALECT)

    def connect(self) -> None:
        self._connection.open()

    def close(self) -> None:
        self._connection.close()

    def new_session(self) -> Self:
        session = copy.copy(self)
        session._connection = self._connection.sibling()

        return session

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        selected = introspect_module.list_tables(self._cursor, include, exclude)
        self._identities.register(selected)

        return [meta for meta, _ in selected]

    def extract_ddl(self, fqn: str) -> str:
        return ddl_module.extract_ddl(self._cursor, self._identity(fqn))

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        columns = introspect_module.columns(self._cursor, self._identity(fqn))
        self._identities.attach(fqn, columns)

        return columns

    def default_collation(self) -> str:
        return introspect_module.default_collation(self._cursor)

    def introspect_relationships(self, fqn: str) -> list[ForeignKeyMeta]:
        return introspect_module.relationships(self._cursor, self._identity(fqn))

    def introspect_indexes(self, fqn: str) -> list[IndexMeta]:
        return introspect_module.indexes(self._cursor, self._identity(fqn))

    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        return introspect_module.unique_keys(self._cursor, self._identity(fqn))

    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        return introspect_module.physical_layout(self._cursor, self._identity(fqn))

    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]] | None:
        return introspect_module.view_dependencies(self._cursor)

    def extract_comments(self, fqn: str) -> CommentsMeta:
        return introspect_module.comments(self._cursor, self._identity(fqn))

    def estimate_row_count(self, fqn: str) -> int | None:
        return row_count_or_none(
            introspect_module.row_count_estimate(self._cursor, self._identity(fqn)),
        )

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, PhaseA]:
        del config

        return stats_module.compute_base(self._cursor, self._identity(fqn), columns, scope)

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
    ) -> PhaseB:
        return stats_module.compute_columns(
            self._cursor,
            self._identity(fqn),
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
            self._identity(fqn),
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
        return stats_module.probe_grain(
            self._cursor,
            self._identity(fqn),
            columns,
            counts,
            candidates,
            scope,
        )

    def probe_timeline(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        column: str,
        unit: Literal["day", "week", "month"],
        scope: TableScope | None = None,
    ) -> tuple[tuple[str, int], ...]:
        return stats_module.probe_timeline(
            self._cursor,
            self._identity(fqn),
            columns,
            counts,
            column,
            unit,
            scope,
        )

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
            self._identity(fqn),
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
            self._identity(fqn),
            columns,
            counts,
            base,
            candidates,
            scope,
        )

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        return stats_module.materialize(self._cursor, self._identity(fqn), scope)

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        del fqn

        stats_module.release(self._cursor, scope)

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
        sql_type: str | None = None,
    ) -> list[Any]:
        return looks_like_module.sample_distinct(
            self._cursor,
            self._identity(fqn),
            column,
            n,
            scope,
            sql_type,
        )

    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: SketchKind,
        k: int,
    ) -> tuple[int, ...]:
        return sketch_module.compute_key_sketch(
            self._cursor,
            self._identity(fqn),
            column,
            sql_type,
            kind,
            k,
        )

    def compute_normalized_cardinality(
        self,
        fqn: str,
        column: str,
        scope: TableScope | None = None,
    ) -> int:
        return normalization_module.compute_normalized_cardinality(
            self._cursor,
            self._identity(fqn),
            column,
            scope,
        )

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run user-authored SQL via the cursor; return all rows (ASSERTIONS.md 3) - read-only
        is the operator's responsibility, not enforced here (ASSERTIONS.md 3.4).
        """

        cursor = exec_query(self._connection.cursor, sql)
        rows = cursor.fetchall()

        return [tuple(row) for row in rows]

    def _identity(self, fqn: str) -> Identity:
        return self._identities[fqn]

    @property
    def _cursor(self) -> Any:
        if not self._connection.is_open():
            raise DuckdbConnectionError("adapter is not connected; call connect() first")

        return self._connection.cursor
