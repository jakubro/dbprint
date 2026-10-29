"""A Ctrl-C during a Snowflake statement cancels it on the server and ends the run at once.

The stand-in connector blocks like connector 5's core: a statement waits on a SQLite lock with the
GIL released and runs no Python signal handler until it returns.
"""

from __future__ import annotations

import importlib
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dbprint.adapters import interrupt
from dbprint.adapters.errors import QueryFailed
from dbprint.adapters.snowflake import connection as connection_module
from dbprint.adapters.snowflake.connection import Connection, ConnectionParams, exec_query
from tests.adapters._credentials import SNOWFLAKE_CREDS


SLOW = "SELECT COUNT(*) FROM TABLE(GENERATOR(TIMELIMIT => 10))"
TIMED_OUT = "SELECT SYSTEM$WAIT(700)"
PANICKING = "SELECT SYSTEM$ABORT_SESSION()"
SESSION_ID = 7319
HOLD_SECONDS = 10


class _ServerError(Exception):
    def __init__(self, errno: int) -> None:
        super().__init__(f"server error {errno}")
        self.errno = errno


class _Panic(BaseException):
    pass


class _Server:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.ends: list[str] = []
        self.cancels: list[Any] = []
        self._lock = threading.Lock()
        self._holder: sqlite3.Connection | None = sqlite3.connect(
            path,
            isolation_level=None,
            check_same_thread=False,
        )
        self._holder.execute("BEGIN EXCLUSIVE")
        self._timer = threading.Timer(HOLD_SECONDS, self.release)
        self._timer.start()

    def cancel(self, session: Any) -> None:
        self.cancels.append(session)

        if session == SESSION_ID:
            self.release()

    def release(self) -> None:
        with self._lock:
            if self._holder is not None:
                self._holder.execute("ROLLBACK")
                self._holder.close()
                self._holder = None

    def close(self) -> None:
        self._timer.cancel()
        self.release()


class _Cursor:
    def __init__(self, server: _Server) -> None:
        self._server = server
        self._rows: list[tuple[Any, ...]] = []
        self.description: Any = None

    def execute(self, sql: str, params: Any = None) -> _Cursor:
        if sql == SLOW:
            waiter = sqlite3.connect(self._server.path, timeout=60, isolation_level=None)

            try:
                waiter.execute("BEGIN EXCLUSIVE")
                waiter.execute("ROLLBACK")
            finally:
                waiter.close()

            if self._server.cancels:
                self._server.ends.append("cancelled")

                raise _ServerError(604)

            self._server.ends.append("completed")
        elif sql == "SELECT SYSTEM$CANCEL_ALL_QUERIES(?)":
            self._server.cancel(params[0])
        elif sql == TIMED_OUT:
            raise _ServerError(630)
        elif sql == PANICKING:
            raise _Panic

        self._rows = [(1,)]
        self.description = (("V",),)

        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        pass


class _Connector:
    def __init__(self, server: _Server) -> None:
        self._server = server

    def connect(self, **_kwargs: object) -> _Connector:
        return self

    @property
    def session_id(self) -> int:
        return SESSION_ID

    def cursor(self) -> _Cursor:
        return _Cursor(self._server)

    def close(self) -> None:
        pass


@pytest.fixture
def server(tmp_path: Path) -> Iterator[_Server]:
    held = _Server(tmp_path / "server.sqlite")

    try:
        yield held
    finally:
        held.close()


@pytest.fixture
def session(server: _Server, monkeypatch: pytest.MonkeyPatch) -> Iterator[Connection]:
    monkeypatch.setattr(importlib, "import_module", lambda _name: _Connector(server))
    opened = Connection(
        ConnectionParams.from_credentials(SNOWFLAKE_CREDS),
    )
    opened.open()

    try:
        yield opened
    finally:
        opened.close()


def test_a_ctrl_c_on_the_main_thread_cancels_the_statement_and_raises_at_once(
    session: Connection,
    server: _Server,
    armed: Callable[[], None],
) -> None:
    started = time.monotonic()

    with pytest.raises(KeyboardInterrupt) as raised:
        _run_on_the_main_thread(session, armed)

    assert time.monotonic() - started < 5
    assert isinstance(raised.value, interrupt.Interrupted)
    assert server.ends == ["cancelled"]
    assert server.cancels == [SESSION_ID]
    assert isinstance(raised.value.__cause__, _ServerError)
    assert raised.value.__cause__.errno == 604
    assert isinstance(raised.value.__cause__.__cause__, KeyboardInterrupt)


def test_a_ctrl_c_cancels_a_statement_waiting_on_a_session_worker(
    session: Connection,
    server: _Server,
    armed: Callable[[], None],
) -> None:
    started = time.monotonic()
    outcome: list[BaseException | None] = []

    with pytest.raises(KeyboardInterrupt):
        _run_on_a_worker(session, armed, outcome)

    assert time.monotonic() - started < 5
    assert server.ends == ["cancelled"]
    assert isinstance(outcome[0], interrupt.Interrupted)


def test_a_ctrl_c_as_the_statement_starts_still_cancels_it(
    session: Connection,
    server: _Server,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        connection_module,
        "threading",
        SimpleNamespace(Event=threading.Event, Thread=_InterruptedOnStart),
    )

    with pytest.raises(KeyboardInterrupt):
        exec_query(session.cursor, SLOW)

    assert server.cancels == [SESSION_ID]
    assert server.ends == ["cancelled"]


def test_a_connector_panic_reaches_the_caller_unchanged(session: Connection) -> None:
    with pytest.raises(_Panic):
        exec_query(session.cursor, PANICKING)


def test_without_a_ctrl_c_a_statement_timeout_is_a_timed_out_failure(session: Connection) -> None:
    with pytest.raises(QueryFailed) as raised:
        exec_query(session.cursor, TIMED_OUT)

    assert raised.value.timed_out


def test_without_a_ctrl_c_a_statement_returns_its_rows(session: Connection) -> None:
    assert exec_query(session.cursor, "SELECT 1").fetchall() == [(1,)]


class _InterruptedOnStart(threading.Thread):
    def start(self) -> None:
        super().start()

        raise KeyboardInterrupt


def _run_on_the_main_thread(session: Connection, arm: Callable[[], None]) -> None:
    with interrupt.handling():
        arm()
        exec_query(session.cursor, SLOW)


def _run_on_a_worker(
    session: Connection,
    arm: Callable[[], None],
    outcome: list[BaseException | None],
) -> None:
    # The main thread stays inside the interruptible span until the worker ends, as a session
    # pool waits out its units; leaving it would clear the recorded Ctrl-C the worker reads.
    with ThreadPoolExecutor(max_workers=1) as pool, interrupt.handling():
        arm()
        future = pool.submit(exec_query, session.cursor, SLOW)

        try:
            future.result()
        finally:
            outcome.append(future.exception(timeout=HOLD_SECONDS))
