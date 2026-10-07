"""ClickhouseAdapter - concrete Adapter wired to the clickhouse helper modules, built from a
credentials dict; every method asserts the session is open before delegating.
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
    ClickhouseConnectionError,
    Connection,
    ConnectionParams,
)
from ..base import (
    CommentsMeta,
    SkippedNamespace,
    SqlAdapter,
    TableMerging,
    TableMeta,
    TableScope,
    row_count_or_none,
)
from ..dialect import Dialect
from ..driver import CursorFactory, ServerParams
from ..identifiers import IdentityRegistry


class SamplingKeyMissing(RuntimeError):
    """Raised when `SAMPLE` is requested against a table with no declared `SAMPLE BY` key.

    The driver raises `SAMPLING_NOT_SUPPORTED` for the same reason, but only after issuing the
    `CREATE TEMPORARY TABLE`; this is caught from the catalog fact `list_tables` already read.
    """


class ClickhouseAdapter(SqlAdapter):
    """Concrete Adapter for ClickHouse, backed by clickhouse-connect's DB-API.

    Preconditions: `list_tables` before extraction, `introspect_columns` before a column-keyed call.
    """

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    SERVER_PARAMS: ClassVar[type[ServerParams]] = ConnectionParams
    # SAMPLE's determinism depends on a declared SAMPLE BY key; an unmaterialized scope on a
    # table without one is not a seeded per-row guarantee.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = False

    DIALECT: ClassVar[Dialect] = DIALECT
    _driver = connection_module
    _introspect = introspect_module
    _ddl = ddl_module
    _stats = stats_module
    _looks_like = looks_like_module
    _sketch = sketch_module
    _normalization = normalization_module
    _not_connected = ClickhouseConnectionError

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
        # Populated by list_tables()'s own read of system.tables.sampling_key - materialize_scope
        # reads from here rather than discovering the same fact by a failed CREATE.
        self._samplable: dict[str, bool] = {}
        self._identities = IdentityRegistry(DIALECT)
        self._skipped: tuple[SkippedNamespace, ...] = ()
        self._final: bool | None = None
        self._shows_secrets: bool | None = None

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        databases = (
            (self._params.database,)
            if self._params.database is not None
            else introspect_module.list_databases(self._cursor)
        )
        selected, samplable, skipped = introspect_module.list_tables(
            self._cursor,
            databases,
            include,
            exclude,
        )
        self._samplable = samplable
        listed = self._register(selected)
        self._skipped = skipped

        return listed

    def skipped_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return self._skipped

    def extract_ddl(self, fqn: str) -> str:
        return ddl_module.extract_ddl(
            self._handle(fqn),
            self._identity(fqn),
            hide_secrets=self._session_shows_secrets(),
        )

    def extract_comments(self, fqn: str) -> CommentsMeta:
        return introspect_module.comments(
            self._handle(fqn),
            self._identity(fqn),
            hide_secrets=self._session_shows_secrets(),
        )

    def introspect_merging(self, fqn: str) -> TableMerging | None:
        if self._final is None:
            self._final = introspect_module.final_reads(self._cursor)

        return introspect_module.merging(self._handle(fqn), self._identity(fqn), final=self._final)

    def estimate_row_count(self, fqn: str) -> int | None:
        return row_count_or_none(
            introspect_module.estimate_row_count(self._cursor, self._identity(fqn)),
        )

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        """Copy a sampled draw - `scope.sample` is always set (base.py's own contract)."""

        if not self._samplable.get(fqn, False):
            raise SamplingKeyMissing(
                f"table {fqn!r} declares no SAMPLE BY key (or is not a MergeTree-family "
                f"table), so ClickHouse's SAMPLE clause is not available on it at any "
                f"fraction.",
            )

        return stats_module.materialize(self._cursor, self._identity(fqn), scope)

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run user-authored SQL and return all rows; SQL assertion path (ASSERTIONS.md 3) -
        read-only is the operator's responsibility, not enforced here (ASSERTIONS.md 3.4).
        """

        cursor = self._cursor
        cursor.execute(sql)
        rows = cursor.fetchall()

        return [tuple(row) for row in rows]

    def _session_shows_secrets(self) -> bool:
        if self._shows_secrets is None:
            self._shows_secrets = introspect_module.shows_secrets(self._cursor)

        return self._shows_secrets
