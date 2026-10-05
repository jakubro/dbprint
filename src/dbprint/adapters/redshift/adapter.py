"""RedshiftAdapter - concrete Adapter wired to the redshift helper modules, built from a
credentials dict; `cursor_factory` substitutes a shim, there being no local substrate.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, ClassVar

from dbprint.spec.classification import is_string_like_type
from . import connection as connection_module
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
    RedshiftConnectionError,
)
from ..base import (
    ColumnMeta,
    PerDatabaseSqlAdapter,
    PhysicalLayout,
    SkippedNamespace,
    TableMeta,
    row_count_or_none,
)
from ..dialect import Dialect
from ..driver import CursorFactory
from ..errors import QueryFailed
from ..identifiers import Identity, IdentityRegistry


# The session a connection with no `database` opens first, to enumerate: it exists on every
# cluster and serverless namespace, and can be neither dropped nor renamed.
ENTRY_DATABASE = "dev"


class RedshiftAdapter(PerDatabaseSqlAdapter):
    """Concrete Adapter for Amazon Redshift backed by redshift-connector.

    Precondition: `list_tables` before extraction; it records the spelling the catalog stores.
    """

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("host", "user", "password")
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = ("database", "port")
    # RANDOM() carries no seed at all, so an unmaterialized `sample` scope redraws with no
    # guarantee of agreement across statements.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = False

    DIALECT: ClassVar[Dialect] = DIALECT
    _driver = connection_module
    _introspect = introspect_module
    _ddl = ddl_module
    _stats = stats_module
    _looks_like = looks_like_module
    _sketch = sketch_module
    _normalization = normalization_module
    _not_connected = RedshiftConnectionError

    def __init__(
        self,
        credentials: dict[str, str],
        cursor_factory: CursorFactory[ConnectionParams] | None = None,
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
        self._start_sessions(self._params.database or ENTRY_DATABASE)
        self._database_names: tuple[str, ...] | None = None
        self._collations: dict[str, str] = {}
        self._identities = IdentityRegistry(DIALECT)
        self._skipped: tuple[SkippedNamespace, ...] = ()
        self._selected_databases: tuple[str, ...] = ()
        self._unread_dependencies: tuple[SkippedNamespace, ...] = ()
        self._external: frozenset[str] = frozenset()

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
        self._external = frozenset(meta.fqn for meta, _ in reachable if meta.external)
        self._selected_databases = tuple(sorted({physical[0] for _, physical in reachable}))

        return [meta for meta, _ in reachable]

    def skipped_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return self._skipped

    def extract_ddl(self, fqn: str) -> str:
        if fqn in self._external:
            return ddl_module.extract_external_ddl(self._handle(fqn), self._identity(fqn))

        return super().extract_ddl(fqn)

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        identity = self._identity(fqn)
        columns = self._columns_of(fqn)(self._handle(fqn), identity)
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

    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        if fqn in self._external:
            return introspect_module.external_physical_layout(
                self._handle(fqn),
                self._identity(fqn),
            )

        return super().introspect_physical_layout(fqn)

    def estimate_row_count(self, fqn: str) -> int | None:
        return row_count_or_none(
            introspect_module.estimate_row_count(self._handle(fqn), self._identity(fqn)),
        )

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

    def _unopened(self, database: str) -> Connection:
        return Connection(replace(self._params, database=database), self._factory)

    def _handle(self, fqn: str) -> Any:
        """The session of the table's own database - never the entry session for another one,
        whose schema-and-name catalog filters would silently read a same-named relation.
        """

        return self._session(self._identity(fqn).parts[0]).cursor

    def _collation(self, database: str) -> str:
        if database not in self._collations:
            cursor = self._session(database).cursor
            self._collations[database] = introspect_module.default_collation(cursor)

        return self._collations[database]

    def _read_identity(self, fqn: str) -> Identity:
        """The identity carrying its columns, read from the catalog if not yet introspected."""

        identity = self._identity(fqn)

        if identity.columns:
            return identity

        return self._identities.attach(
            fqn,
            self._columns_of(fqn)(self._handle(fqn), identity),
        )

    def _columns_of(self, fqn: str) -> Callable[[Any, Identity], list[ColumnMeta]]:
        return (
            introspect_module.external_columns
            if fqn in self._external
            else introspect_module.columns
        )
