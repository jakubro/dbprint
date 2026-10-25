"""A Ctrl-C a driver reports as an ordinary error still ends the run, through the statement seam."""

from __future__ import annotations

import os
import signal
import threading
import time
from collections.abc import Callable, Iterator

import pytest

from dbprint.adapters import interrupt
from dbprint.adapters.duckdb.connection import Connection, ConnectionParams, exec_query
from dbprint.adapters.errors import QueryFailed


SLOW = "SELECT SUM(hash(rng.i)) FROM range(100000000000) rng (i)"


@pytest.fixture
def connection() -> Iterator[Connection]:
    opened = Connection(ConnectionParams(":memory:", statement_timeout=60))
    opened.open()

    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def armed() -> Iterator[Callable[[], None]]:
    timers: list[threading.Timer] = []

    def arm() -> None:
        timers.append(threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT)))
        timers[-1].start()

    try:
        yield arm
    finally:
        for timer in timers:
            timer.cancel()


def test_inside_handling_a_converted_interrupt_raises_interrupted_promptly(
    connection: Connection,
    armed: Callable[[], None],
) -> None:
    seen: list[bool] = []
    started = time.monotonic()

    with pytest.raises(interrupt.Interrupted):
        _run_handled(connection, armed, seen, swallow=False)

    assert time.monotonic() - started < 5
    assert seen == [True]


def test_with_no_handler_the_chained_interrupt_alone_raises_interrupted(
    connection: Connection,
    armed: Callable[[], None],
) -> None:
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    armed()

    try:
        with pytest.raises(interrupt.Interrupted):
            exec_query(connection.cursor, SLOW)
    finally:
        signal.signal(signal.SIGINT, previous)


def test_a_statement_timeout_is_a_timed_out_failure_never_an_interrupt() -> None:
    timed = Connection(ConnectionParams(":memory:", statement_timeout=1))
    timed.open()

    try:
        with pytest.raises(QueryFailed) as raised:
            exec_query(timed.cursor, SLOW)
    finally:
        timed.close()

    assert raised.value.timed_out


def test_handling_restores_the_handler_installed_before_it() -> None:
    before = signal.getsignal(signal.SIGINT)

    with interrupt.handling():
        assert signal.getsignal(signal.SIGINT) is not before

    assert signal.getsignal(signal.SIGINT) is before


def test_a_block_that_swallows_the_interrupt_still_ends_interrupted(
    connection: Connection,
    armed: Callable[[], None],
) -> None:
    seen: list[bool] = []

    with pytest.raises(interrupt.Interrupted):
        _run_handled(connection, armed, seen, swallow=True)

    assert seen == [True]


def _run_handled(
    connection: Connection,
    arm: Callable[[], None],
    seen: list[bool],
    *,
    swallow: bool,
) -> None:
    with interrupt.handling():
        arm()

        try:
            exec_query(connection.cursor, SLOW)
        except BaseException:
            seen.append(interrupt.requested())

            if not swallow:
                raise
