"""Query-failure wrapper carrying the statement that raised.

Each adapter's `exec_query` wraps a driver exception in `QueryFailed` so the statement and
its bound parameters travel with the error. The engine renders the one-line form onto the
per-table result; the CLI renders `detail()` in its grouped failure report.
"""

from __future__ import annotations

from typing import Any


_SQL_MAX_LINES = 12
_SQL_MAX_CHARS = 600
_PARAMS_MAX_CHARS = 200
_TRUNCATED = "... (truncated)"


class QueryFailed(RuntimeError):
    """A database statement raised; carries the statement and its parameters."""

    def __init__(self, cause: BaseException, sql: str, params: Any = None) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.sql = sql
        self.params = params

    def __str__(self) -> str:
        return f"{type(self.cause).__name__}: {self.cause}"

    def detail(self) -> str:
        """Render the statement and its parameters as an indented block, clipped."""

        body = "\n".join(f"    {line}" for line in _clip_sql(self.sql).splitlines())
        lines = ["  statement:", body]

        if self.params is not None:
            lines.append(f"  params: {_clip(repr(self.params), _PARAMS_MAX_CHARS)}")

        return "\n".join(lines)

    def detail_untruncated(self) -> str:
        """Render the statement and its parameters as an indented block, unclipped."""

        body = "\n".join(f"    {line}" for line in _dedent_sql(self.sql).splitlines())
        lines = ["  statement:", body]

        if self.params is not None:
            lines.append(f"  params: {self.params!r}")

        return "\n".join(lines)


def _dedent_sql(sql: str) -> str:
    """Strip per-line indentation and blank lines, keeping content only."""

    lines = [line.strip() for line in sql.strip().splitlines() if line.strip()]

    return "\n".join(lines)


def _clip_sql(sql: str) -> str:
    """Dedent, then clip to the line / character budget."""

    lines = _dedent_sql(sql).splitlines()

    if len(lines) > _SQL_MAX_LINES:
        lines = [*lines[:_SQL_MAX_LINES], _TRUNCATED]

    return _clip("\n".join(lines), _SQL_MAX_CHARS)


def _clip(text: str, limit: int) -> str:
    """Bound `text` to `limit` characters, marking any truncation."""

    if len(text) <= limit:
        return text

    return text[:limit] + _TRUNCATED
