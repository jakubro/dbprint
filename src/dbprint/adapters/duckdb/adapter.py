"""DuckdbAdapter - concrete Adapter wired to the duckdb helper modules, built from a
credentials dict; every method asserts the connection is open before delegating.
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
    DuckdbConnectionError,
)
from ..base import (
    SqlAdapter,
    TableMeta,
)
from ..dialect import Dialect
from ..driver import CursorFactory
from ..identifiers import IdentityRegistry


class DuckdbAdapter(SqlAdapter):
    """Concrete Adapter for duckdb, in-process - no server, no separate driver process."""

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("database",)
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = ("read_only",)
    PATH_KEYS: ClassVar[tuple[str, ...]] = ("database",)
    # Same BERNOULLI guarantee as Postgres - an unmaterialized `sample` scope stays coherent.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = True

    DIALECT: ClassVar[Dialect] = DIALECT
    _driver = connection_module
    _introspect = introspect_module
    _ddl = ddl_module
    _stats = stats_module
    _looks_like = looks_like_module
    _sketch = sketch_module
    _normalization = normalization_module
    _not_connected = DuckdbConnectionError

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

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        selected = introspect_module.list_tables(self._cursor, include, exclude)
        listed = self._register(selected)

        return listed
