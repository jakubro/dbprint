"""SnowflakeAdapter - concrete Adapter wired to the snowflake helper modules.

Constructed from a credentials dict the engine fills from `REQUIRED_KEYS`. `cursor_factory`
substitutes the cursor: production builds a snowflake-connector one, tests inject a duckdb
connection satisfying the same DB-API surface.
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
from .connection import DIALECT, Connection, ConnectionParams, CursorFactory, exec_query
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
    SkippedNamespace,
    StatisticsConfig,
    TableCounts,
    TableMeta,
    TableScope,
    UniqueKeyMeta,
    row_count_or_none,
)
from ..errors import QueryFailed
from ..identifiers import Identity, IdentityRegistry


class SnowflakeAdapter(Adapter):
    """Concrete Adapter for Snowflake backed by snowflake-connector-python.

    Snowflake reports identifiers in physical (usually uppercase) case while dbprint
    addresses objects by lowercased path segments, so the adapter records the physical form
    as `list_tables`/`introspect_columns` observe it: `list_tables` must run first.
    """

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("account", "user", "warehouse", "role")
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = (
        "database",
        "password",
        "private_key_file",
        "private_key_file_pwd",
        "schema",
    )
    PATH_KEYS: ClassVar[tuple[str, ...]] = ("private_key_file",)
    # Block sampling's seed guarantee does not cover an unmaterialized re-evaluation, so a
    # `sample` scope with no copy must be refused rather than measured over drifting rows.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = False

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
        self._skipped: tuple[SkippedNamespace, ...] = ()
        self._selected_databases: tuple[str, ...] = ()
        self._unread_dependencies: tuple[SkippedNamespace, ...] = ()
        self._database_names: tuple[str, ...] | None = None

    def connect(self) -> None:
        self._connection.open()

    def close(self) -> None:
        self._connection.close()

    def new_session(self) -> Self:
        session = copy.copy(self)
        session._connection = self._connection.sibling()

        return session

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        selected, skipped = introspect_module.list_tables(
            self._cursor,
            self._databases(),
            include,
            exclude,
        )
        self._identities.register(selected)
        self._skipped = skipped
        self._selected_databases = tuple(sorted({physical[0] for _, physical in selected}))

        return [meta for meta, _ in selected]

    def skipped_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return self._skipped

    def extract_ddl(self, fqn: str) -> str:
        return ddl_module.extract_ddl(self._cursor, self._identity(fqn))

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        metas = introspect_module.columns(self._cursor, self._identity(fqn))
        self._identities.attach(fqn, metas)

        return metas

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
        out: dict[str, tuple[str, ...]] = {}
        unread: list[SkippedNamespace] = []

        for database in self._selected_databases:
            try:
                out.update(introspect_module.view_dependencies(self._cursor, (database,)))
            except QueryFailed as exc:
                unread.append(SkippedNamespace(name=database, cause=str(exc)))

        self._unread_dependencies = tuple(unread)

        return out

    def unread_dependency_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return self._unread_dependencies

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
        stats_module.release(self._cursor, self._identity(fqn), scope)

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
        """Run user-authored SQL via the cursor; return all rows (ASSERTIONS.md 3).

        Read-only is the operator's responsibility, not enforced here (ASSERTIONS.md 3.4).
        """

        cursor = exec_query(self._cursor, sql)
        rows = cursor.fetchall()

        return [tuple(row) for row in rows]

    def _databases(self) -> tuple[str, ...]:
        """The databases read, resolved once; a configured name takes the catalog's own spelling,
        since it is bound and quoted from here on where the connector used to fold it.
        """

        if self._database_names is None:
            configured = self._params.database

            if configured is None:
                self._database_names = introspect_module.list_databases(self._cursor)
            else:
                matches = [
                    name
                    for name in introspect_module.list_databases(self._cursor, like=configured)
                    if name.upper() == configured.upper()
                ]
                exact = [name for name in matches if name == configured]
                self._database_names = (
                    (exact or matches)[0] if len(exact or matches) == 1 else configured,
                )

        return self._database_names

    def _identity(self, fqn: str) -> Identity:
        return self._identities[fqn]

    @property
    def _cursor(self) -> Any:
        return self._connection.cursor
