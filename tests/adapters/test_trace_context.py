"""adapters.trace_context - contextvar defaults and the shared statement-trace log helpers."""

from __future__ import annotations

import logging

import pytest

from dbprint.adapters import trace_context
from dbprint.adapters.errors import QueryFailed


_LOG = logging.getLogger("dbprint.adapters.test_trace_context")


class TestDefaults:
    def test_unset_vars_read_as_empty_strings(self) -> None:
        assert trace_context.connection.get() == ""
        assert trace_context.fqn.get() == ""
        assert trace_context.phase.get() == ""


class TestLogSuccess:
    def test_carries_statement_params_elapsed_and_rows(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            trace_context.log_success(_LOG, 0.0, "SELECT %s", ("x",), 3)

        assert "SELECT %s" in caplog.text
        assert "('x',)" in caplog.text
        assert "rows=3" in caplog.text
        assert "elapsed_ms=" in caplog.text

    def test_sql_and_params_are_not_merged(self, caplog: pytest.LogCaptureFixture) -> None:
        """A placeholder in the text must stay a placeholder - params ride alongside, not in it."""

        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            trace_context.log_success(_LOG, 0.0, "SELECT %s", ("secret",), None)

        sql_field = next(part for part in caplog.text.split() if part.startswith("sql="))
        assert "secret" not in sql_field

    @pytest.mark.parametrize("rowcount", [None, -1, [("a", "b")]])
    def test_a_meaningless_rowcount_is_omitted(
        self,
        caplog: pytest.LogCaptureFixture,
        rowcount: object,
    ) -> None:
        """`.rowcount` is read via `getattr`; anything but a non-negative int must not raise."""

        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            trace_context.log_success(_LOG, 0.0, "SELECT 1", None, rowcount)

        assert "rows=-" in caplog.text

    def test_tags_read_from_the_current_context(self, caplog: pytest.LogCaptureFixture) -> None:
        conn_token = trace_context.connection.set("primary")
        fqn_token = trace_context.fqn.set("public.t")
        phase_token = trace_context.phase.set("extract_ddl")

        try:
            with caplog.at_level(logging.DEBUG, logger=_LOG.name):
                trace_context.log_success(_LOG, 0.0, "SELECT 1", None, None)
        finally:
            trace_context.connection.reset(conn_token)
            trace_context.fqn.reset(fqn_token)
            trace_context.phase.reset(phase_token)

        assert "conn=primary" in caplog.text
        assert "fqn=public.t" in caplog.text
        assert "phase=extract_ddl" in caplog.text


class TestLogFailure:
    def test_statement_is_untruncated_past_the_console_clip(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        long_sql = "\n".join(f"line {i}" for i in range(40))
        failure = QueryFailed(RuntimeError("boom"), long_sql)

        with caplog.at_level(logging.DEBUG, logger=_LOG.name):
            trace_context.log_failure(_LOG, 0.0, failure)

        assert "line 39" in caplog.text
        assert "truncated" not in caplog.text
