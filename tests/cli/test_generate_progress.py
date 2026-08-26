"""Progress rendering for `dbprint generate` - streaming through the CLI end-to-end, live
against a forced terminal on its observable contract rather than pixels.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Self
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from dbprint.adapters import ColumnMeta, ColumnStats, CommentsMeta, Inferred, MockAdapter, MockTable
from dbprint.cli.main import main
from dbprint.cli.rendering.progress import (
    ConnectionSummary,
    LiveProgressRenderer,
    StreamingProgressRenderer,
    _eta_seconds,
    build_progress_renderer,
    install_log_handler,
    remove_log_handler,
)
from dbprint.engine import DiffSummary, GenerateResult, ProgressEvent, SummaryCounts, TableResult
from dbprint.engine.result import ProgressPhase, ProgressStatus


_ZERO_DIFF = DiffSummary(
    tables_added=0,
    tables_removed=0,
    tables_modified=0,
    columns_added=0,
    columns_removed=0,
    columns_type_changed=0,
    columns_nullable_changed=0,
    columns_default_changed=0,
    statistics_drifted=0,
    relationships_changed=0,
    indexes_changed=0,
    comments_changed=0,
    unchanged_tables=0,
    unevaluated_tables=0,
)

PROJECT_YAML = """\
defaults:
  max_age_days: 7
  statistics: {}
  diff: {}
connections:
  primary:
    adapter: postgres
    auto: true
    output: prints
"""


def _result(
    connection: str,
    *,
    ok: int,
    failed: int,
    skipped: int,
    elapsed_ms: int,
    tables: tuple[TableResult, ...] = (),
) -> GenerateResult:
    return GenerateResult(
        connection_name=connection,
        tables=tables,
        summary=SummaryCounts(ok=ok, skipped=skipped, failed=failed),
        diff_summary=_ZERO_DIFF,
        elapsed_ms=elapsed_ms,
        exit_code=0,
    )


def _event(
    phase: ProgressPhase,
    status: ProgressStatus,
    *,
    fqn: str | None = None,
    column: str | None = None,
    column_index: int | None = None,
    column_total: int | None = None,
    elapsed_ms: int | None = None,
    row_count: int | None = None,
    error: str | None = None,
) -> ProgressEvent:
    return ProgressEvent(
        connection="acme",
        phase=phase,
        status=status,
        index=1,
        total=1,
        fqn=fqn,
        column=column,
        column_index=column_index,
        column_total=column_total,
        elapsed_ms=elapsed_ms,
        row_count=row_count,
        error=error,
    )


def _terminal_event(
    phase: ProgressPhase,
    status: ProgressStatus,
    index: int,
    total: int,
    elapsed_ms: int | None,
) -> ProgressEvent:
    return ProgressEvent(
        connection="acme",
        phase=phase,
        status=status,
        index=index,
        total=total,
        fqn=f"s.t{index}",
        elapsed_ms=elapsed_ms,
    )


class TestStreamingRenderer:
    def test_start_then_terminal_lines_no_phase_noise(self) -> None:
        buf = StringIO()

        with StreamingProgressRenderer(buf) as r:
            r.on_event(_event("extract", "start", fqn="s.t"))
            r.on_event(
                _event(
                    "statistics",
                    "start",
                    fqn="s.t",
                    column="id",
                    column_index=1,
                    column_total=1,
                ),
            )
            r.on_event(_event("write", "done", fqn="s.t", elapsed_ms=1500, row_count=4106))
            r.on_event(_event("extract", "failed", fqn="s.u", error="boom"))

        lines = buf.getvalue().splitlines()

        assert "acme\ts.t\tstart" in lines
        assert any(line.startswith("acme\ts.t\tok\t") for line in lines)
        assert "acme\ts.u\tfailed\tboom" in lines
        assert not any("statistics" in line for line in lines)

    def test_finalizing_events_emit_nothing(self) -> None:
        buf = StringIO()

        with StreamingProgressRenderer(buf) as r:
            r.on_event(_event("finalizing", "start", fqn=None))
            r.on_event(_event("finalizing", "done", fqn=None))

        assert buf.getvalue() == ""

    def test_summary_line_preserves_the_prior_contract(self) -> None:
        buf = StringIO()
        StreamingProgressRenderer(buf).connection_summary(
            _result("acme", ok=149, failed=1, skipped=0, elapsed_ms=94500),
        )

        assert buf.getvalue() == "acme\tsummary\t149 ok / 1 failed / 0 skipped\t94500ms\n"

    def test_prepass_phases_reach_the_piped_stream(self) -> None:
        buf = StringIO()

        with StreamingProgressRenderer(buf) as r:
            r.on_event(_event("connecting", "start"))
            r.on_event(_event("connecting", "done"))
            r.on_event(_event("listing", "start"))
            r.on_event(_event("listing", "done"))
            r.on_event(_event("inventory", "start"))

        lines = buf.getvalue().splitlines()

        assert "acme\tconnecting\tstart" in lines
        assert "acme\tconnecting\tdone" in lines
        assert "acme\tlisting\tstart" in lines
        assert "acme\tlisting\tdone" in lines
        assert "acme\tinventory\tstart\t1/1" in lines

    def test_finish_writes_nothing(self) -> None:
        buf = StringIO()

        with StreamingProgressRenderer(buf) as r:
            r.on_event(_event("extract", "start", fqn="s.t"))
            r.finish()

        assert buf.getvalue() == "acme\ts.t\tstart\n"


class TestLiveRenderer:
    def test_streams_tree_headers_then_truncated_leaf(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)
        long_fqn = "fixture.public." + "x" * 80

        with LiveProgressRenderer(console) as r:
            r.on_event(_event("extract", "start", fqn=long_fqn))
            r.on_event(
                _event(
                    "statistics",
                    "start",
                    fqn=long_fqn,
                    column="created_at",
                    column_index=1,
                    column_total=3,
                ),
            )
            r.on_event(_event("write", "done", fqn=long_fqn, elapsed_ms=1200, row_count=12330))

        out = buf.getvalue()

        assert "acme" in out  # connection header
        assert "fixture" in out  # database header
        assert "public" in out  # schema header
        assert "x" * 80 not in out  # leaf tail-truncated
        assert "12,330 rows" in out  # ok shown by counts (no status word, no color)

    def test_sibling_tables_share_headers_without_reemit(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(
                _event("write", "done", fqn="fieldwork.alpha", elapsed_ms=100, row_count=1),
            )
            r.on_event(
                _event("write", "done", fqn="fieldwork.beta", elapsed_ms=100, row_count=2),
            )

        out = buf.getvalue()

        assert out.count("acme") == 1  # connection header
        assert out.count("fieldwork") == 1  # schema header
        assert "alpha" in out
        assert "beta" in out

    def test_skipped_and_failed_leaves_are_distinguishable_without_color(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(_event("extract", "skipped", fqn="fixture.fresh"))
            r.on_event(_event("extract", "failed", fqn="fixture.broken", error="boom detail"))

        out = buf.getvalue()

        assert "(skipped)" in out
        assert "boom detail" in out

    def test_prepass_advances_the_label_without_moving_the_bar(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(_event("connecting", "start"))
            r.on_event(_event("connecting", "done"))
            r.on_event(_event("listing", "start"))
            r.on_event(ProgressEvent(connection="acme", phase="listing", status="done", total=40))
            r.on_event(
                ProgressEvent(connection="acme", phase="inventory", status="start", total=40),
            )
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="inventory",
                    status="start",
                    index=7,
                    total=40,
                ),
            )
            # `index` stays bound to the table loop, not the pre-pass object count.
            assert r._index == 0
            assert r._total == 40
            assert "inventory" in r._inflight_line().plain
            assert "7/40" in r._inflight_line().plain

            r.on_event(_event("extract", "start", fqn="s.t"))
            assert r._index == 1
            assert r._fqn == "s.t"

    def test_summary_lists_failed_tables(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)
        failed = TableResult(fqn="s.audit", status="failed", error="boom", elapsed_ms=10)
        LiveProgressRenderer(console).connection_summary(
            _result("acme", ok=1, failed=1, skipped=0, elapsed_ms=100, tables=(failed,)),
        )

        out = buf.getvalue()

        assert "acme" in out
        assert "failed: s.audit" in out
        # The cause belongs to the grouped report, not repeated per failed table.
        assert "boom" not in out

    def test_finish_on_a_clean_run_reads_completed(self) -> None:
        # Checks what finish() hands console.print: Rich's cursor escapes defeat substring search.
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with (
            patch.object(console, "print", wraps=console.print) as mock_print,
            LiveProgressRenderer(console) as r,
        ):
            r.on_event(_event("extract", "start", fqn="s.t"))
            r.on_event(_event("write", "done", fqn="s.t", elapsed_ms=10, row_count=1))
            r.connection_summary(_result("acme", ok=1, failed=0, skipped=0, elapsed_ms=10))
            r.finish()

        final = str(mock_print.call_args_list[-1].args[0])

        assert "Completed" in final
        assert "Completed with failures" not in final
        assert "Profiling" not in final

    def test_finish_on_a_run_with_failures_reads_completed_with_failures(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)
        failed = TableResult(fqn="s.bad", status="failed", error="boom", elapsed_ms=10)

        with LiveProgressRenderer(console) as r:
            r.connection_summary(
                _result("acme", ok=0, failed=1, skipped=0, elapsed_ms=10, tables=(failed,)),
            )
            r.finish()

        assert "Completed with failures" in buf.getvalue()

    def test_finish_treats_a_connection_that_never_reached_a_table_as_failed(self) -> None:
        """`summary.failed` is 0 for a connection error - `error` is the only signal."""

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.connection_summary(
                ConnectionSummary(
                    connection_name="acme",
                    summary=SummaryCounts(ok=0, failed=0, skipped=0),
                    elapsed_ms=5,
                    error="connection refused",
                ),
            )
            r.finish()

        assert "Completed with failures" in buf.getvalue()

    def test_finish_is_idempotent(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.finish()
            r.finish()

        assert buf.getvalue().count("Completed") == 1

    def test_no_in_flight_line_survives_into_the_final_frame(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with (
            patch.object(console, "print", wraps=console.print) as mock_print,
            LiveProgressRenderer(console) as r,
        ):
            r.on_event(_event("extract", "start", fqn="s.t"))
            r.finish()

        final = str(mock_print.call_args_list[-1].args[0])

        assert "s.t" not in final
        assert "extract ddl" not in final
        assert "Completed" in final


class TestETA:
    """The bar's remaining-time estimate, accumulated per terminal state."""

    def test_no_eta_before_any_table_finishes(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            assert "ETA --:--" in r._bar_line().plain

    def test_finalizing_does_not_corrupt_the_accumulator(self) -> None:
        """`finalizing("done", ...)` reaches the terminal branch with no table attached."""

        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(
                ProgressEvent(connection="acme", phase="finalizing", status="done", total=0),
            )

            assert r._terminal_counts == {"done": 0, "skipped": 0, "failed": 0}
            assert "ETA --:--" in r._bar_line().plain

    def test_eta_is_remaining_times_the_observed_mean(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 21):
                r.on_event(_terminal_event("write", "done", i, 100, 5000))

            # 80 tables remain at an observed mean of 5s each.
            assert "ETA 0:06:40" in r._bar_line().plain

    def test_a_run_of_skips_does_not_collapse_the_estimate(self) -> None:
        """The ETA estimate is not dominated by the most recent (all-skip) run."""

        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 21):
                r.on_event(_terminal_event("write", "done", i, 100, 5000))

            for i in range(21, 71):
                r.on_event(_terminal_event("extract", "skipped", i, 100, 10))

            eta = _eta_seconds(r._terminal_counts, r._durations, 100 - 70)

            assert eta is not None
            assert eta > 20

    def test_a_failed_tables_duration_is_counted(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 21):
                r.on_event(_terminal_event("write", "done", i, 100, 5000))

            eta_before = _eta_seconds(r._terminal_counts, r._durations, 100 - 20)
            r.on_event(_terminal_event("extract", "failed", 21, 100, 60_000))
            eta_after = _eta_seconds(r._terminal_counts, r._durations, 100 - 21)

            assert eta_before is not None
            assert eta_after is not None
            assert eta_after > eta_before

    def test_no_eta_on_the_final_frame(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with (
            patch.object(console, "print", wraps=console.print) as mock_print,
            LiveProgressRenderer(console) as r,
        ):
            r.on_event(_event("write", "done", fqn="s.t", elapsed_ms=10, row_count=1))
            r.finish()

        final = str(mock_print.call_args_list[-1].args[0])

        assert "ETA" not in final

    def test_streaming_renderer_carries_no_eta(self) -> None:
        """The piped path stays byte-for-byte the tab-separated contract it always was."""

        buf = StringIO()

        with StreamingProgressRenderer(buf) as r:
            r.on_event(_event("write", "done", fqn="s.t", elapsed_ms=10, row_count=1))

        assert "ETA" not in buf.getvalue()


class TestLogRouting:
    """A warning prints under the table it describes, not ahead of it."""

    def test_table_scoped_warning_attaches_after_its_leaf(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(_event("extract", "start", fqn="fixture.unsized"))
            r.log_record(
                "no row-count estimate for 'fixture.unsized'; "
                "rules carrying `min_rows` do not apply to it",
            )
            r.on_event(_event("extract", "skipped", fqn="fixture.unsized"))

        lines = [line for line in buf.getvalue().splitlines() if line.strip()]
        leaf_index = next(
            i for i, line in enumerate(lines) if "unsized" in line and "skipped" in line
        )

        assert leaf_index + 1 < len(lines)
        note = lines[leaf_index + 1]
        assert "no row-count estimate" in note
        # The leaf line above it already carries the fqn - redundant here.
        assert "fixture.unsized" not in note
        assert "rules carrying" in note

    def test_several_warnings_for_one_table_print_in_the_order_raised(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(_event("extract", "start", fqn="s.t"))
            r.log_record("first warning")
            r.log_record("second warning")
            r.on_event(_event("write", "done", fqn="s.t", elapsed_ms=1, row_count=1))

        out = buf.getvalue()

        assert out.index("first warning") < out.index("second warning")

    def test_pre_pass_warning_holds_until_the_connection_summary(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(_event("inventory", "start"))  # no table in flight
            r.log_record("catalog pre-pass introspect_columns failed for 'public.ghost': boom")
            r.connection_summary(_result("acme", ok=0, failed=0, skipped=0, elapsed_ms=5))

        out = buf.getvalue()

        assert "introspect_columns failed" in out
        # Held warnings are not table-scoped, so the fqn is not stripped.
        assert "public.ghost" in out
        assert out.index("acme") < out.index("introspect_columns failed")

    def test_held_warnings_do_not_leak_into_the_next_connection(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.log_record("first connection warning")
            r.connection_summary(_result("acme", ok=0, failed=0, skipped=0, elapsed_ms=5))
            r.connection_summary(_result("beta", ok=0, failed=0, skipped=0, elapsed_ms=5))

        out = buf.getvalue()

        assert out.count("first connection warning") == 1

    def test_finish_flushes_any_warning_left_unflushed(self) -> None:
        """Defense in depth: nothing normally reaches finish() with warnings still held."""

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.log_record("orphaned warning")
            r.finish()

        assert "orphaned warning" in buf.getvalue()

    def test_streaming_renderer_writes_the_line_with_no_tree(
        self,
        capsys: pytest.CaptureFixture,
    ) -> None:
        with StreamingProgressRenderer(StringIO()) as r:
            r.log_record("no row-count estimate for 'public.t'; rules do not apply")

        captured = capsys.readouterr()

        assert captured.out == ""
        assert "no row-count estimate" in captured.err

    def test_install_and_remove_route_a_real_log_record(self) -> None:
        import logging

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)
        logger = logging.getLogger("dbprint.engine.orchestrator")

        with LiveProgressRenderer(console) as r:
            handler = install_log_handler(r)

            try:
                logger.warning("adapter close failed for connection 'acme': boom")
            finally:
                remove_log_handler(handler)

            r.connection_summary(_result("acme", ok=0, failed=0, skipped=0, elapsed_ms=5))

        assert "adapter close failed" in buf.getvalue()

    def test_removed_handler_stops_forwarding(self) -> None:
        import logging

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)
        logger = logging.getLogger("dbprint.engine.orchestrator")

        with LiveProgressRenderer(console) as r:
            handler = install_log_handler(r)
            remove_log_handler(handler)
            logger.warning("should not reach the renderer")
            r.connection_summary(_result("acme", ok=0, failed=0, skipped=0, elapsed_ms=5))

        assert "should not reach the renderer" not in buf.getvalue()


class TestRendererSelection:
    def test_no_tui_selects_streaming(self) -> None:
        renderer = build_progress_renderer(live=False, console=Console(), out=StringIO())

        assert isinstance(renderer, StreamingProgressRenderer)

    def test_live_on_non_terminal_degrades_to_streaming(self) -> None:
        console = Console(file=StringIO())
        renderer = build_progress_renderer(live=True, console=console, out=StringIO())

        assert isinstance(renderer, StreamingProgressRenderer)

    def test_live_on_terminal_selects_live(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80)
        renderer = build_progress_renderer(live=True, console=console, out=StringIO())

        assert isinstance(renderer, LiveProgressRenderer)


class _MockPostgresAdapter(MockAdapter):
    REQUIRED_KEYS = ("host", "port", "database", "user", "password")

    def __init__(self, _credentials: dict[str, str]) -> None:
        super().__init__(_two_table_fixture())


def _two_table_fixture() -> dict[str, MockTable]:
    return {"public.a": _table("public", "a"), "public.b": _table("public", "b")}


def _table(schema: str, name: str) -> MockTable:
    return MockTable(
        type="table",
        namespace_path=(schema, name),
        ddl=f"CREATE TABLE {schema}.{name} (id uuid PRIMARY KEY);\n",
        columns=[ColumnMeta(name="id", sql_type="uuid", nullable=False, default=None, ordinal=1)],
        relationships=[],
        indexes=[],
        comments=CommentsMeta(table=None, columns={}),
        stats={
            "id": ColumnStats(
                sql_type="uuid",
                nullable=False,
                null_count=0,
                null_rate=0.0,
                cardinality=10,
                cardinality_ratio=1.0,
                cardinality_method="exact",
                inferred=Inferred(candidate_key=True),
            ),
        },
        samples={"id": [f"00000000-0000-7000-8000-{i:012d}" for i in range(10)]},
        row_count=10,
    )


def _credential_env() -> dict[str, str]:
    return {
        "DBPRINT_PRIMARY_HOST": "h",
        "DBPRINT_PRIMARY_PORT": "5432",
        "DBPRINT_PRIMARY_DATABASE": "d",
        "DBPRINT_PRIMARY_USER": "u",
        "DBPRINT_PRIMARY_PASSWORD": "p",
    }


class TestPipedStreamingThroughCli:
    def test_start_lines_stream_before_summary(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        runner = CliRunner()

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _MockPostgresAdapter},
            clear=True,
        ):
            result = runner.invoke(main, ["generate", "--no-tui"])

        out = result.output

        assert "public.a\tstart" in out
        assert "public.a\tok" in out
        assert out.index("start") < out.index("summary")

    def test_failure_block_still_reaches_piped_output(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Deferring stderr writes for the live renderer must not touch the streaming path."""

        (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        class _BrokenAdapter(MockAdapter):
            REQUIRED_KEYS = ("host", "port", "database", "user", "password")

            def __init__(self, _credentials: dict[str, str]) -> None:
                super().__init__({"public.a": _table("public", "a")})

            def compute_column_statistics(self, fqn: str, *args: object, **kwargs: object):
                raise RuntimeError("boom")

        runner = CliRunner()

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _BrokenAdapter},
            clear=True,
        ):
            result = runner.invoke(main, ["generate", "--no-tui"])

        assert "1 table failed: RuntimeError: boom" in result.output


class _RecordingRenderer:
    """Fake `ProgressRenderer` recording the call sequence, pinning `connection_summary` first."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def on_event(self, event: ProgressEvent) -> None:
        self.calls.append(f"on_event:{event.connection}")

    def connection_summary(self, result: GenerateResult | ConnectionSummary) -> None:
        self.calls.append(f"connection_summary:{result.connection_name}")

    def flush_warnings(self) -> None:
        self.calls.append("flush_warnings")

    def finish(self) -> None:
        self.calls.append("finish")

    def log_record(self, text: str) -> None:
        self.calls.append(f"log_record:{text}")


class TestFlushWarningsCalledPerConnection:
    def test_the_one_connection_gets_exactly_one_flush(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        recorder = _RecordingRenderer()
        monkeypatch.setattr(
            "dbprint.cli.commands.generate.build_progress_renderer",
            lambda **kwargs: recorder,
        )

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _MockPostgresAdapter},
            clear=True,
        ):
            CliRunner().invoke(main, ["generate"])

        assert recorder.calls.count("flush_warnings") == 1
        # The flush follows the summary it is a backstop for.
        summary_index = recorder.calls.index("connection_summary:primary")
        flush_index = recorder.calls.index("flush_warnings")

        assert summary_index < flush_index
