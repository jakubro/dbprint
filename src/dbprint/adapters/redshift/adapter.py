"""RedshiftAdapter - concrete Adapter wired to the redshift helper modules, built from a
credentials dict; `cursor_factory` substitutes a shim, there being no local substrate.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any, ClassVar, Literal, Self

from dbprint.spec.classification import is_string_like_type
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
    RedshiftConnectionError,
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


# The session a connection with no `database` opens first, to enumerate: it exists on every
# cluster and serverless namespace, and can be neither dropped nor renamed.
ENTRY_DATABASE = "dev"


class RedshiftAdapter(Adapter):
    """Concrete Adapter for Amazon Redshift backed by redshift-connector.

    Precondition: `list_tables` before extraction; it records the spelling the catalog stores.
    """

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("host", "user", "password")
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = ("database", "port")
    # RANDOM() carries no seed at all, so an unmaterialized `sample` scope redraws with no
    # guarantee of agreement across statements.
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
        # A session reads one database's catalog alone (cross-database queries do not reach
        # pg_catalog), so each database a table lives in gets its own, opened on first use.
        self._factory = cursor_factory
        self._entry_database = self._params.database or ENTRY_DATABASE
        self._connection = Connection(
            replace(self._params, database=self._entry_database),
            cursor_factory,
        )
        self._sessions: dict[str, Connection] = {self._entry_database: self._connection}
        self._database_names: tuple[str, ...] | None = None
        self._collations: dict[str, str] = {}
        self._identities = IdentityRegistry(DIALECT)
        self._skipped: tuple[SkippedNamespace, ...] = ()
        self._selected_databases: tuple[str, ...] = ()
        self._unread_dependencies: tuple[SkippedNamespace, ...] = ()

    def connect(self) -> None:
        self._connection.open()

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()

        self._sessions = {self._entry_database: self._connection}

    def new_session(self) -> Self:
        session = copy.copy(self)
        session._connection = self._connection.sibling()
        session._sessions = {self._entry_database: session._connection}

        return session

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        selected = introspect_module.list_tables(
            self._cursor,
            self._databases(),
            include,
            exclude,
        )
        skipped: list[SkippedNamespace] = []

        for database in sorted({physical[0] for _, physical in selected}):
            try:
                self._session(database)
            except RedshiftConnectionError as exc:
                skipped.append(SkippedNamespace(name=database, cause=str(exc)))

        lost = {entry.name for entry in skipped}
        reachable = [(meta, physical) for meta, physical in selected if physical[0] not in lost]
        self._identities.register(reachable)
        self._skipped = tuple(skipped)
        self._selected_databases = tuple(sorted({physical[0] for _, physical in reachable}))

        return [meta for meta, _ in reachable]

    def skipped_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return self._skipped

    def extract_ddl(self, fqn: str) -> str:
        return ddl_module.extract_ddl(self._cursor_for(fqn), self._identity(fqn))

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        identity = self._identity(fqn)
        columns = introspect_module.columns(self._cursor_for(fqn), identity)
        self._identities.attach(fqn, columns)
        own = self._collation(identity.parts[0])

        # The manifest states the entry database's collation; a table compared under another
        # database's default says so on each string column, or it would inherit the wrong one.
        if own == self._collation(self._entry_database):
            return columns

        return [
            replace(c, collation=own)
            if c.collation is None and is_string_like_type(c.sql_type)
            else c
            for c in columns
        ]

    def default_collation(self) -> str:
        return introspect_module.default_collation(self._cursor)

    def introspect_relationships(self, fqn: str) -> list[ForeignKeyMeta]:
        return introspect_module.relationships(self._cursor_for(fqn), self._identity(fqn))

    def introspect_indexes(self, fqn: str) -> list[IndexMeta]:
        return introspect_module.indexes(self._cursor_for(fqn), self._identity(fqn))

    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        return introspect_module.unique_keys(self._cursor_for(fqn), self._identity(fqn))

    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        return introspect_module.physical_layout(self._cursor_for(fqn), self._identity(fqn))

    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]] | None:
        out: dict[str, tuple[str, ...]] = {}
        unread: list[SkippedNamespace] = []

        for database in self._selected_databases:
            try:
                cursor = self._session(database).cursor
                out.update(introspect_module.view_dependencies(cursor, database))
            except (RedshiftConnectionError, QueryFailed) as exc:
                unread.append(SkippedNamespace(name=database, cause=str(exc)))

        self._unread_dependencies = tuple(unread)

        return out

    def unread_dependency_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return self._unread_dependencies

    def extract_comments(self, fqn: str) -> CommentsMeta:
        return introspect_module.comments(self._cursor_for(fqn), self._identity(fqn))

    def estimate_row_count(self, fqn: str) -> int | None:
        return row_count_or_none(
            introspect_module.estimate_row_count(self._cursor_for(fqn), self._identity(fqn)),
        )

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, PhaseA]:
        del config

        return stats_module.compute_base(self._cursor_for(fqn), self._identity(fqn), columns, scope)

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
            self._cursor_for(fqn),
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
            self._cursor_for(fqn),
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
            self._cursor_for(fqn),
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
            self._cursor_for(fqn),
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
            self._cursor_for(fqn),
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
            self._cursor_for(fqn),
            self._identity(fqn),
            columns,
            counts,
            base,
            candidates,
            scope,
        )

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        return stats_module.materialize(self._cursor_for(fqn), self._identity(fqn), scope)

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        stats_module.release(self._cursor_for(fqn), scope)

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
        sql_type: str | None = None,
    ) -> list[Any]:
        return looks_like_module.sample_distinct(
            self._cursor_for(fqn),
            self._columned(fqn),
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
            self._cursor_for(fqn),
            self._columned(fqn),
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
            self._cursor_for(fqn),
            self._columned(fqn),
            column,
            scope,
        )

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run user-authored SQL and return all rows; SQL assertion path (ASSERTIONS.md 3) -
        read-only is the operator's responsibility, not enforced here (ASSERTIONS.md 3.4).
        """

        cursor = exec_query(self._cursor, sql)
        rows = cursor.fetchall()

        return [tuple(row) for row in rows]

    def _databases(self) -> tuple[str, ...]:
        """The databases read, resolved once; a configured name takes the catalog's own spelling,
        since `svv_redshift_tables` compares it as stored.
        """

        if self._database_names is None:
            configured = self._params.database
            listed = introspect_module.list_databases(self._cursor)

            if configured is None:
                self._database_names = listed
            else:
                matches = [name for name in listed if name.lower() == configured.lower()]
                exact = [name for name in matches if name == configured]
                self._database_names = (
                    (exact or matches)[0] if len(exact or matches) == 1 else configured,
                )

        return self._database_names

    def _session(self, database: str) -> Connection:
        session = self._sessions.get(database)

        if session is None:
            session = Connection(replace(self._params, database=database), self._factory)
            session.open()
            self._sessions[database] = session

        return session

    def _cursor_for(self, fqn: str) -> Any:
        """The session of the table's own database - never the entry session for another one,
        whose schema-and-name catalog filters would silently read a same-named relation.
        """

        return self._session(self._identity(fqn).parts[0]).cursor

    def _collation(self, database: str) -> str:
        if database not in self._collations:
            cursor = self._session(database).cursor
            self._collations[database] = introspect_module.default_collation(cursor)

        return self._collations[database]

    def _identity(self, fqn: str) -> Identity:
        return self._identities[fqn]

    def _columned(self, fqn: str) -> Identity:
        """The identity carrying its columns, read from the catalog if not yet introspected."""

        identity = self._identity(fqn)

        if identity.columns:
            return identity

        return self._identities.attach(
            fqn,
            introspect_module.columns(self._cursor_for(fqn), identity),
        )

    @property
    def _cursor(self) -> Any:
        if not self._connection.is_open():
            raise RedshiftConnectionError("adapter is not connected; call connect() first")

        return self._connection.cursor
