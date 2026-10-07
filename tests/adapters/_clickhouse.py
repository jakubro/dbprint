"""A stand-in ClickHouse cursor recording every statement it is handed."""

from __future__ import annotations

from typing import Any


class Recorder:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = None) -> Any:
        self.statements.append(" ".join(sql.split()))

        return self._cursor.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)
