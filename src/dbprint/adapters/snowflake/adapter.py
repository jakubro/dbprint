"""SnowflakeAdapter - concrete Adapter wired to the snowflake helper modules.

Constructed from a credentials dict the engine fills from `REQUIRED_KEYS`. `cursor_factory`
substitutes the cursor: production builds a snowflake-connector one, tests inject a duckdb
connection satisfying the same DB-API surface.
"""

from __future__ import annotations

from typing import Any, ClassVar

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
    SnowflakeConnectionError,
)
from ..base import (
    SkippedNamespace,
    SqlAdapter,
    TableMeta,
    TableScope,
)
from ..dialect import Dialect
from ..driver import CursorFactory
from ..errors import QueryFailed
from ..identifiers import IdentityRegistry


class SnowflakeAdapter(SqlAdapter):
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

    DIALECT: ClassVar[Dialect] = DIALECT
    _driver = connection_module
    _introspect = introspect_module
    _ddl = ddl_module
    _stats = stats_module
    _looks_like = looks_like_module
    _sketch = sketch_module
    _normalization = normalization_module
    _not_connected = SnowflakeConnectionError

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
        self._connection = Connection(self._params, cursor_factory)
        self._identities = IdentityRegistry(DIALECT)
        self._skipped: tuple[SkippedNamespace, ...] = ()
        self._selected_databases: tuple[str, ...] = ()
        self._unread_dependencies: tuple[SkippedNamespace, ...] = ()
        self._database_names: tuple[str, ...] | None = None

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

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        stats_module.release(self._cursor, self._identity(fqn), scope)

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

    @property
    def _cursor(self) -> Any:
        return self._connection.cursor
