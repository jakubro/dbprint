"""DatabricksAdapter - concrete Adapter wired to the databricks helper modules, built from a
credentials dict; `connect()` probes `information_schema` once and every method dispatches on it.
"""

from __future__ import annotations

from typing import ClassVar

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
    DatabricksConnectionError,
)
from ..base import (
    ColumnMeta,
    ForeignKeyMeta,
    SkippedNamespace,
    SqlAdapter,
    TableMeta,
    UniqueKeyMeta,
)
from ..dialect import Dialect
from ..driver import CursorFactory
from ..identifiers import IdentityRegistry


class DatabricksAdapter(SqlAdapter):
    """Concrete Adapter for Databricks backed by databricks-sql-connector."""

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("server_hostname", "http_path", "access_token")
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = ("catalog",)
    # TABLESAMPLE ... REPEATABLE is coherent on this engine (measured, stats.py's `_source`),
    # so an unmaterialized `sample` scope still reads stably across statements.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = True

    DIALECT: ClassVar[Dialect] = DIALECT
    _driver = connection_module
    _introspect = introspect_module
    _ddl = ddl_module
    _stats = stats_module
    _looks_like = looks_like_module
    _sketch = sketch_module
    _normalization = normalization_module
    _not_connected = DatabricksConnectionError

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
        self._unity_catalog = False
        self._catalogs: tuple[str, ...] = ()
        self._skipped: tuple[SkippedNamespace, ...] = ()

    def connect(self) -> None:
        self._connection.open()
        catalog = self._params.catalog
        self._unity_catalog = introspect_module.detect_unity_catalog(self._cursor, catalog)

        if catalog is not None:
            self._catalogs = (catalog,)
        elif self._unity_catalog:
            self._catalogs = introspect_module.list_catalogs(self._cursor)
        else:
            self._catalogs = (introspect_module.session_catalog(self._cursor),)

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        selected, self._skipped = introspect_module.list_tables(
            self._cursor,
            include,
            exclude,
            unity_catalog=self._unity_catalog,
            catalogs=self._catalogs,
        )
        listed = self._register(selected)

        return listed

    def skipped_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return self._skipped

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        columns = introspect_module.columns(
            self._cursor,
            self._identity(fqn),
            unity_catalog=self._unity_catalog,
        )
        self._identities.attach(fqn, columns)

        return columns

    def introspect_relationships(self, fqn: str) -> list[ForeignKeyMeta]:
        return introspect_module.relationships(
            self._cursor,
            self._identity(fqn),
            unity_catalog=self._unity_catalog,
        )

    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        return introspect_module.unique_keys(
            self._cursor,
            self._identity(fqn),
            unity_catalog=self._unity_catalog,
        )

    def estimate_row_count(self, fqn: str) -> int | None:
        return introspect_module.estimate_row_count(self._cursor, self._identity(fqn))
