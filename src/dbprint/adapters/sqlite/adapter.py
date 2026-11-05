"""SqliteAdapter - concrete Adapter wired to the SQLite helper modules, built from a credentials
dict; every method asserts the connection is open before delegating.
"""

from __future__ import annotations

import re
import sqlite3
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
    SqliteConnectionError,
)
from ..base import ColumnMeta, SqlAdapter, TableMeta
from ..dialect import Dialect
from ..driver import CursorFactory
from ..errors import NoResultSet
from ..identifiers import IdentityRegistry
from ...spec.sketch import SketchKind


_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


class SqliteAdapter(SqlAdapter):
    """Concrete Adapter for SQLite, in-process - the file always opened read-only."""

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("database",)
    PATH_KEYS: ClassVar[tuple[str, ...]] = ("database",)

    DIALECT: ClassVar[Dialect] = DIALECT
    _driver = connection_module
    _introspect = introspect_module
    _ddl = ddl_module
    _stats = stats_module
    _looks_like = looks_like_module
    _sketch = sketch_module
    _normalization = normalization_module
    _not_connected = SqliteConnectionError

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
        self._views: tuple[str, ...] = ()

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        selected = introspect_module.list_tables(self._cursor, include, exclude)
        listed = self._register(selected)
        self._views = tuple(meta.fqn for meta in listed if meta.type == "view")

        return listed

    def read_columns(self, fqn: str, columns: list[ColumnMeta]) -> list[ColumnMeta]:
        return introspect_module.read_columns(self._handle(fqn), self._identity(fqn), columns)

    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]]:
        views = [self._identity(fqn) for fqn in self._views]

        return introspect_module.view_dependencies(self._cursor, views)

    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: SketchKind,
        k: int,
    ) -> tuple[int, ...]:
        """`h` is a signed int64 over the full unsigned pattern; `(h < 0), h` sorts it unsigned."""

        hashes = self._key_sketch(fqn, column, sql_type, kind, k, order="(h < 0), h")

        return tuple(sketch_module.unsigned(h) for h in hashes)

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run each statement of `sql` in turn on the session and return the last one's rows."""

        last: Any = None

        for statement in _statements(sql):
            last = connection_module.exec_query(self._cursor, statement)

        if last is None or last.description is None:
            raise NoResultSet

        return [tuple(row) for row in last.fetchall()]


def _statements(sql: str) -> list[str]:
    """`sql` cut after each `;` that ends a statement - never one inside a literal or a comment,
    which `complete_statement` reads as unfinished - less any piece holding no statement.
    """

    pieces: list[str] = []
    start = 0

    for at, char in enumerate(sql):
        if char == ";" and sqlite3.complete_statement(sql[start : at + 1]):
            pieces.append(sql[start : at + 1])
            start = at + 1

    pieces.append(sql[start:])

    return [piece for piece in pieces if _COMMENT_RE.sub("", piece).strip(" \t\r\n;")]
