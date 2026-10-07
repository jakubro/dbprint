"""PostgresAdapter - concrete Adapter wired to the postgres helper modules.

Constructed from a credentials dict the engine fills from `REQUIRED_KEYS`; every method
asserts the connection is open before delegating to a helper module.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, ClassVar, LiteralString, cast

from dbprint.config import selectors
from . import connection as connection_module
from . import ddl as ddl_module
from . import introspect as introspect_module
from . import looks_like as looks_like_module
from . import normalization as normalization_module
from . import sketch as sketch_module
from . import stats as stats_module
from .connection import DIALECT, Connection, ConnectionParams, PostgresConnectionError, exec_query
from ..base import (
    ColumnMeta,
    PerDatabaseSqlAdapter,
    PhaseA,
    SkippedNamespace,
    StatisticsConfig,
    TableCounts,
    TableMeta,
    TableScope,
    row_count_or_none,
)
from ..dialect import Dialect
from ..driver import ServerParams
from ..errors import QueryFailed
from ..identifiers import IdentityRegistry, select_tables


# The database a connection with no `database` connects to first, to enumerate the others.
ENTRY_DATABASE = "postgres"


class PostgresAdapter(PerDatabaseSqlAdapter):
    """Concrete Adapter for PostgreSQL backed by psycopg3 + pg_dump.

    Precondition: `list_tables` before extraction; it records the spelling `pg_class` compares.
    """

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    SERVER_PARAMS: ClassVar[type[ServerParams]] = ConnectionParams
    # BERNOULLI decides membership per row by hashing (block, offset, seed) - an
    # unmaterialized `sample` scope still reads the same rows on every statement.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = True

    DIALECT: ClassVar[Dialect] = DIALECT
    _driver = connection_module
    _introspect = introspect_module
    _ddl = ddl_module
    _stats = stats_module
    _looks_like = looks_like_module
    _sketch = sketch_module
    _normalization = normalization_module
    _not_connected = PostgresConnectionError

    def __init__(
        self,
        credentials: dict[str, str],
        *,
        statement_timeout: int | None = None,
    ) -> None:
        self._params = ConnectionParams.from_credentials(
            credentials,
            statement_timeout=statement_timeout,
        )
        # A session is bound to one database, so each database a table lives in gets its own,
        # opened on first use and held for the run: a materialized sample outlives extraction.
        self._start_sessions(self._params.database or ENTRY_DATABASE)
        self._identities = IdentityRegistry(DIALECT)
        self._skipped: tuple[SkippedNamespace, ...] = ()
        self._selected_databases: tuple[str, ...] = ()
        self._unread_dependencies: tuple[SkippedNamespace, ...] = ()
        self._foreign: frozenset[str] = frozenset()

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        candidates = []
        skipped: list[SkippedNamespace] = []

        for database in self._databases():
            if not selectors.may_hold(database, include):
                continue

            try:
                conn = self._session(database).psycopg_connection
                candidates += introspect_module.relations(conn, database)
            except (PostgresConnectionError, QueryFailed) as exc:
                skipped.append(SkippedNamespace(name=database, cause=str(exc)))

        selected = select_tables(candidates, include, exclude)
        listed = self._register(selected)
        self._skipped = tuple(skipped)
        self._selected_databases = tuple(sorted({physical[0] for _, physical in selected}))
        self._foreign = frozenset(meta.fqn for meta, _ in selected if meta.external)

        return listed

    def skipped_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return self._skipped

    def extract_ddl(self, fqn: str) -> str:
        self._require_open()

        return ddl_module.extract_ddl(self._params, self._identity(fqn))

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        identity = self._identity(fqn)
        columns = introspect_module.columns(self._handle(fqn), identity)
        self._identities.attach(fqn, columns)

        return self._with_database_collation(identity, columns)

    def default_collation(self) -> str:
        return introspect_module.default_collation(self._psycopg)

    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]] | None:
        out: dict[str, tuple[str, ...]] = {}
        unread: list[SkippedNamespace] = []

        for database in self._selected_databases:
            try:
                conn = self._session(database).psycopg_connection
                out.update(introspect_module.view_dependencies(conn, database))
            except (PostgresConnectionError, QueryFailed) as exc:
                unread.append(SkippedNamespace(name=database, cause=str(exc)))

        self._unread_dependencies = tuple(unread)

        return out

    def unread_dependency_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return self._unread_dependencies

    def estimate_row_count(self, fqn: str) -> int | None:
        return row_count_or_none(
            introspect_module.reltuples_estimate(self._handle(fqn), self._identity(fqn)),
        )

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, PhaseA]:
        if fqn in self._foreign and scope is not None and scope.sample is not None:
            raise ValueError(
                f"foreign table {fqn} cannot be sampled (TABLESAMPLE applies to local tables "
                f"only); narrow it with a filter rule instead",
            )

        return super().compute_base_statistics(fqn, columns, config, scope)

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        if fqn in self._foreign:
            return scope

        return super().materialize_scope(fqn, scope)

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
        sql_type: str | None = None,
    ) -> list[Any]:
        return looks_like_module.sample_distinct(
            self._handle(fqn),
            self._read_identity(fqn),
            column,
            n,
            scope,
            sql_type,
            foreign=fqn in self._foreign,
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

    def _databases(self) -> tuple[str, ...]:
        if self._params.database is not None:
            return (self._params.database,)

        return introspect_module.list_databases(self._psycopg)

    def _unopened(self, database: str) -> Connection:
        return Connection(replace(self._params, database=database))

    def _handle(self, fqn: str) -> Any:
        return self._session(self._identity(fqn).parts[0]).psycopg_connection

    def _session_handle(self, session: Connection) -> Any:
        return session.psycopg_connection

    @property
    def _psycopg(self):
        self._require_open()

        return self._connection.psycopg_connection

    def _require_open(self) -> None:
        if not self._connection.is_open():
            raise PostgresConnectionError("adapter is not connected; call connect() first")
