"""Statement-trace tags and the DEBUG record every adapter's exec_query emits from them.

The engine sets the three tags; `exec_query` takes `(cursor, sql, params)` and would otherwise
know none of them. Each caller passes its own module logger, so a reader can filter by adapter.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from logging import Logger
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from .errors import QueryFailed


connection: ContextVar[str] = ContextVar("dbprint_trace_connection", default="")
fqn: ContextVar[str] = ContextVar("dbprint_trace_fqn", default="")
phase: ContextVar[str] = ContextVar("dbprint_trace_phase", default="")


def log_success(
    logger: Logger,
    started: float,
    sql: str,
    params: Any,
    rowcount: Any,
) -> None:
    """DEBUG record for a statement that returned: text, params (unmerged), elapsed, rows.

    `rowcount` is whatever the driver exposed - the `Cursor` Protocol does not declare it - so
    only a non-negative `int` is logged as a count.
    """

    logger.debug(
        "statement conn=%s fqn=%s phase=%s elapsed_ms=%d rows=%s sql=%r params=%r",
        connection.get(),
        fqn.get(),
        phase.get(),
        _elapsed_ms(started),
        rowcount if isinstance(rowcount, int) and rowcount >= 0 else "-",
        sql,
        params,
    )


def log_failure(logger: Logger, started: float, failure: QueryFailed) -> None:
    """DEBUG record for a raised statement, untruncated, logged before `QueryFailed` propagates."""

    logger.debug(
        "statement failed conn=%s fqn=%s phase=%s elapsed_ms=%d\n%s",
        connection.get(),
        fqn.get(),
        phase.get(),
        _elapsed_ms(started),
        failure.detail_untruncated(),
    )


def _elapsed_ms(started: float) -> int:
    """Milliseconds elapsed since `started` (a `time.monotonic()` timestamp)."""

    return int((time.monotonic() - started) * 1000)
