"""MysqlAdapter - concrete Adapter wired to the mysql helper modules.

Constructed from a credentials dict the engine fills from `REQUIRED_KEYS`; methods assert the
session is open first. The wire protocol is served by MariaDB (test substrate) and Oracle MySQL.
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
from .connection import DIALECT, Connection, ConnectionParams, MysqlConnectionError
from ..base import (
    ColumnMeta,
    PhaseA,
    SqlAdapter,
    StatisticsConfig,
    TableCounts,
    TableMeta,
    TableScope,
    row_count_or_none,
)
from ..dialect import Dialect
from ..identifiers import IdentityRegistry


class MysqlAdapter(SqlAdapter):
    """Concrete Adapter for MySQL / MariaDB backed by mysql-connector-python.

    Precondition: `list_tables` before extraction; it records the spelling the catalog compares.
    """

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("host", "port", "user", "password")
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = ("database",)
    # RAND(seed) is undocumented across multiple references in one statement, so a
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
    _not_connected = MysqlConnectionError

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
        self._connection = Connection(self._params)
        self._identities = IdentityRegistry(DIALECT)
        self._versioned: frozenset[str] = frozenset()

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        selected, self._versioned = introspect_module.list_tables(
            self._cursor,
            self._params.database,
            include,
            exclude,
        )
        self._identities.register(selected)

        return [meta for meta, _ in selected]

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, PhaseA]:
        del config

        return stats_module.compute_base(
            self._handle(fqn),
            self._identity(fqn),
            columns,
            scope,
            exact_count=fqn in self._versioned,
        )

    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]] | None:
        # MariaDB has no `view_table_usage`, and a dependency parsed out of DDL is not published.
        if self._connection.mariadb:
            return None

        return introspect_module.view_dependencies(self._cursor, self._params.database)

    def estimate_row_count(self, fqn: str) -> int | None:
        return row_count_or_none(
            introspect_module.table_rows_estimate(self._cursor, self._identity(fqn)),
        )
