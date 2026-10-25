"""Stand-in ClickHouse cursors: one recording every statement, one rewriting it as the driver does."""

from __future__ import annotations

from typing import Any

from clickhouse_connect.driver.binding import bind_query


class Recorder:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        self.statements.append(" ".join(sql.split()))

        return self._cursor.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class PyformatCursor:
    """chdb behind clickhouse-connect's DB-API rewrite: bound through `bind_query`, else `%%` -> `%`."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def execute(self, sql: str, params: Any = None) -> Any:
        return self._cursor.execute(
            sql.replace("%%", "%") if not params else bind_query(sql, params)[0],
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)
