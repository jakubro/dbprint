"""A multi-statement SQL assertion runs on Snowflake; each statement dbprint writes runs alone.

The stand-in connector refuses a call holding more than one statement unless the call's statement
count is 0, as the server does at its default count of 1.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from typing import Any

import pytest

from dbprint.adapters import SnowflakeAdapter
from dbprint.adapters.errors import QueryFailed
from dbprint.adapters.snowflake.connection import Connection, ConnectionParams, exec_query
from tests.adapters._credentials import SNOWFLAKE_CREDS


class _ServerError(Exception):
    def __init__(self, errno: int) -> None:
        super().__init__(f"server error {errno}")
        self.errno = errno


class _Cursor:
    def __init__(self, counts: list[int | None]) -> None:
        self._counts = counts
        self._results: list[list[tuple[Any, ...]]] = []
        self.description: Any = None

    def execute(self, sql: str, params: Any = None, num_statements: int | None = None) -> None:
        del params
        self._counts.append(num_statements)
        statements = [part.strip() for part in sql.split(";") if part.strip()]

        if len(statements) > 1 and num_statements != 0:
            raise _ServerError(8)

        self._results = [[(int(statement.split()[1]),)] for statement in statements]
        self.description = (("V",),)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._results[0]

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._results[0][0]

    def nextset(self) -> bool:
        self._results = self._results[1:]

        return bool(self._results)

    def close(self) -> None:
        pass


class _Connector:
    def __init__(self) -> None:
        self.counts: list[int | None] = []

    def connect(self, **_kwargs: object) -> _Connector:
        return self

    def cursor(self) -> _Cursor:
        return _Cursor(self.counts)

    def close(self) -> None:
        pass


@pytest.fixture
def connector(monkeypatch: pytest.MonkeyPatch) -> _Connector:
    stand_in = _Connector()
    monkeypatch.setattr(importlib, "import_module", lambda _name: stand_in)

    return stand_in


@pytest.fixture
def adapter(connector: _Connector) -> Iterator[SnowflakeAdapter]:
    del connector
    opened = SnowflakeAdapter(SNOWFLAKE_CREDS)
    opened.connect()

    try:
        yield opened
    finally:
        opened.close()


@pytest.mark.parametrize(
    ("sql", "rows"),
    [("SELECT 1 AS v; SELECT 0 AS v", [(0,)]), ("SELECT 7 AS v", [(7,)])],
)
def test_an_assertion_runs_every_statement_in_one_call_and_answers_with_the_last(
    adapter: SnowflakeAdapter,
    connector: _Connector,
    sql: str,
    rows: list[tuple[int]],
) -> None:
    assert adapter.execute_query(sql) == rows
    assert connector.counts == [0]


def test_a_statement_dbprint_writes_names_no_count(connector: _Connector) -> None:
    session = Connection(ConnectionParams.from_credentials(SNOWFLAKE_CREDS))
    session.open()

    try:
        exec_query(session.cursor, "SELECT 3 AS v")

        with pytest.raises(QueryFailed):
            exec_query(session.cursor, "SELECT 1 AS v; SELECT 0 AS v")
    finally:
        session.close()

    assert connector.counts == [None, None]
