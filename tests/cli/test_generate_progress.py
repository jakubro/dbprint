"""Progress rendering for `dbprint generate` - streaming through the CLI end-to-end, live
against a forced terminal on its observable contract rather than pixels.
"""

from __future__ import annotations

from io import StringIO
from itertools import pairwise
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
from dbprint.engine.orchestrator import _ProgressEmitter
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

    def test_summary_line_carries_counts_and_a_unified_duration(self) -> None:
        """94500ms is over a minute - the connection summary shares `tree.duration_text` with
        every other elapsed time a user sees, not its own bare-millisecond format.
        """

        buf = StringIO()
        StreamingProgressRenderer(buf).connection_summary(
            _result("acme", ok=149, failed=1, skipped=0, elapsed_ms=94500),
        )

        assert buf.getvalue() == "acme\tsummary\t149 ok / 1 failed / 0 skipped\t1m 34s\n"

    def test_prepass_bracket_phases_reach_the_piped_stream(self) -> None:
        buf = StringIO()

        with StreamingProgressRenderer(buf) as r:
            r.on_event(_event("connecting", "start"))
            r.on_event(_event("connecting", "done"))
            r.on_event(_event("listing", "start"))
            r.on_event(_event("listing", "done"))
            r.on_event(ProgressEvent(connection="acme", phase="inventory", status="start", total=1))
            r.on_event(ProgressEvent(connection="acme", phase="inventory", status="done", total=1))

        lines = buf.getvalue().splitlines()

        assert "acme\tconnecting\tstart" in lines
        assert "acme\tconnecting\tdone" in lines
        assert "acme\tlisting\tstart" in lines
        assert "acme\tlisting\tdone" in lines
        assert "acme\tinventory\tstart" in lines
        assert "acme\tinventory\tdone" in lines

    def test_inventory_collapses_to_one_schema_line_not_one_per_object(self) -> None:
        buf = StringIO()

        with StreamingProgressRenderer(buf) as r:
            r.on_event(ProgressEvent(connection="acme", phase="inventory", status="start", total=3))
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="inventory",
                    status="start",
                    index=1,
                    total=3,
                    fqn="seedbank.a",
                ),
            )
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="inventory",
                    status="start",
                    index=2,
                    total=3,
                    fqn="seedbank.b",
                ),
            )
            # seedbank closes here - its schema differs from fieldwork's, one object late.
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="inventory",
                    status="start",
                    index=3,
                    total=3,
                    fqn="fieldwork.c",
                ),
            )
            # fieldwork closes only on the phase's own "done".
            r.on_event(ProgressEvent(connection="acme", phase="inventory", status="done", total=3))

        lines = buf.getvalue().splitlines()
        schema_lines = [line for line in lines if "\tinventory\tschema\t" in line]

        assert len(schema_lines) == 2  # one per schema, not one of the three ticks
        seedbank_line = next(line for line in schema_lines if "\tseedbank\t" in line)
        fieldwork_line = next(line for line in schema_lines if "\tfieldwork\t" in line)
        assert "2 objects" in seedbank_line
        assert "1 objects" in fieldwork_line

    def test_sketch_pass_emits_one_line_per_table_not_per_column(self) -> None:
        buf = StringIO()

        with StreamingProgressRenderer(buf) as r:
            r.on_event(ProgressEvent(connection="acme", phase="sketch", status="start", total=1))
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="sketch",
                    status="start",
                    index=1,
                    total=1,
                    fqn="s.t",
                ),
            )
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="sketch",
                    status="start",
                    index=1,
                    total=1,
                    fqn="s.t",
                    column="id",
                    column_index=1,
                    column_total=1,
                ),
            )
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="sketch",
                    status="done",
                    index=1,
                    total=1,
                    fqn="s.t",
                    elapsed_ms=1500,
                ),
            )

        lines = buf.getvalue().splitlines()

        assert "acme\ts.t\tsketched\t1.5s" in lines
        assert not any("id" in line for line in lines)  # the column tick stays silent

    def test_sketch_table_failure_reaches_the_piped_stream(self) -> None:
        buf = StringIO()

        with StreamingProgressRenderer(buf) as r:
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="sketch",
                    status="failed",
                    index=1,
                    total=1,
                    fqn="s.t",
                    error="boom",
                ),
            )

        assert buf.getvalue() == "acme\ts.t\tsketch_failed\tboom\n"

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

    def test_prepass_advances_the_catalogue_bar_then_hands_it_to_profiling(self) -> None:
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
            assert r._bar_label == "Cataloguing"
            assert r._index == 0
            assert r._total == 40

            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="inventory",
                    status="start",
                    index=7,
                    total=40,
                    fqn="seedbank.accession",
                ),
            )
            assert r._index == 7
            assert r._total == 40
            assert "seedbank" in r._inflight_line().plain

            r.on_event(
                ProgressEvent(connection="acme", phase="inventory", status="done", total=40),
            )

            # The label change is what marks the new pass - an index restart alone reads
            # exactly like a crash and retry.
            r.on_event(_event("extract", "start", fqn="s.t"))
            assert r._bar_label == "Profiling"
            assert r._index == 1
            assert r._fqn == "s.t"

    def test_prepass_closes_a_schema_row_when_the_schema_changes(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(
                ProgressEvent(connection="acme", phase="inventory", status="start", total=3),
            )
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="inventory",
                    status="start",
                    index=1,
                    total=3,
                    fqn="seedbank.a",
                ),
            )
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="inventory",
                    status="start",
                    index=2,
                    total=3,
                    fqn="seedbank.b",
                ),
            )
            # seedbank closes here - two objects seen before fieldwork's first tick.
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="inventory",
                    status="start",
                    index=3,
                    total=3,
                    fqn="fieldwork.c",
                ),
            )
            # fieldwork closes only on the phase's own "done" - never one tick behind.
            r.on_event(
                ProgressEvent(connection="acme", phase="inventory", status="done", total=3),
            )

        out = buf.getvalue()

        assert "seedbank" in out
        assert "2 objects" in out
        assert "fieldwork" in out
        assert "1 objects" in out

    def test_sketch_pass_takes_the_bar_and_banners_the_scrollback(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(
                _event("write", "done", fqn="seedbank.accession", elapsed_ms=10, row_count=1),
            )
            assert r._bar_label == "Profiling"

            r.on_event(
                ProgressEvent(connection="acme", phase="sketch", status="start", total=1),
            )
            assert r._bar_label == "Sketching"
            assert r._index == 0
            assert r._total == 1

            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="sketch",
                    status="start",
                    index=1,
                    total=1,
                    fqn="seedbank.accession",
                ),
            )
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="sketch",
                    status="start",
                    index=1,
                    total=1,
                    fqn="seedbank.accession",
                    column="id",
                    column_index=1,
                    column_total=2,
                ),
            )
            # No "sketch" prefix - it would restate the bar label, which already says Sketching.
            assert "sketch" not in r._inflight_line().plain
            assert "1/2" in r._inflight_line().plain
            assert "id" in r._inflight_line().plain

            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="sketch",
                    status="done",
                    index=1,
                    total=1,
                    fqn="seedbank.accession",
                    elapsed_ms=500,
                ),
            )

        out = buf.getvalue()

        assert "Sketching" in out  # the section banner
        # `_printed_path` resets when the sketch pass starts, so the schema header the
        # Profiling row already printed comes back a second time for the Sketching row.
        assert out.count("seedbank") == 2
        assert out.count("accession") == 2
        # Sketch reads no table rows - a borrowed `- rows` would claim a measurement this
        # phase never took, so the assertion scopes to the Sketching section's own leaf.
        assert "- rows" not in out.rsplit("Sketching", 1)[-1]

    def test_sketch_leaf_carries_its_own_duration(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(
                ProgressEvent(connection="acme", phase="sketch", status="start", total=1),
            )
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="sketch",
                    status="done",
                    index=1,
                    total=1,
                    fqn="seedbank.accession",
                    elapsed_ms=1500,
                ),
            )

        out = buf.getvalue()
        assert "1.5s" in out
        assert "rows" not in out

    def test_a_run_with_no_sketch_event_never_shows_sketching(self) -> None:
        """`dbprint diff` shares this renderer but never emits a `sketch` event, so the label
        change and banner must stay conditioned on that event actually arriving.
        """

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(ProgressEvent(connection="acme", phase="connecting", status="start"))
            r.on_event(ProgressEvent(connection="acme", phase="connecting", status="done"))
            r.on_event(
                _event("write", "done", fqn="seedbank.accession", elapsed_ms=10, row_count=1),
            )
            assert r._bar_label == "Profiling"

            r.on_event(ProgressEvent(connection="acme", phase="finalizing", status="start"))
            r.on_event(ProgressEvent(connection="acme", phase="finalizing", status="done"))
            assert r._bar_label == "Profiling"
            r.finish()

        assert "Sketching" not in buf.getvalue()

    def test_bar_label_never_names_a_phase_that_has_not_begun(self) -> None:
        """The bar reads Connecting, then Listing objects, then Cataloguing - never a guess."""

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(ProgressEvent(connection="acme", phase="connecting", status="start"))
            assert r._bar_label == "Connecting"

            r.on_event(ProgressEvent(connection="acme", phase="connecting", status="done"))
            r.on_event(ProgressEvent(connection="acme", phase="listing", status="start"))
            assert r._bar_label == "Listing objects"

            r.on_event(ProgressEvent(connection="acme", phase="listing", status="done"))
            r.on_event(
                ProgressEvent(connection="acme", phase="inventory", status="start", total=1),
            )
            assert r._bar_label == "Cataloguing"

            r.on_event(
                _event("write", "done", fqn="seedbank.accession", elapsed_ms=10, row_count=1),
            )
            assert r._bar_label == "Profiling"
            r.finish()

    def test_every_streamed_tree_gets_its_own_box(self) -> None:
        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=100, color_system=None)

        with LiveProgressRenderer(console) as r:
            r.on_event(ProgressEvent(connection="acme", phase="connecting", status="start"))
            r.on_event(ProgressEvent(connection="acme", phase="connecting", status="done"))
            r.on_event(ProgressEvent(connection="acme", phase="listing", status="start"))
            r.on_event(ProgressEvent(connection="acme", phase="listing", status="done"))
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="inventory",
                    status="start",
                    total=1,
                ),
            )
            r.on_event(
                ProgressEvent(
                    connection="acme",
                    phase="inventory",
                    status="start",
                    fqn="seedbank.accession",
                    index=1,
                    total=1,
                ),
            )
            r.on_event(ProgressEvent(connection="acme", phase="inventory", status="done"))
            r.on_event(
                _event("write", "done", fqn="seedbank.accession", elapsed_ms=10, row_count=1),
            )
            r.on_event(
                ProgressEvent(connection="acme", phase="sketch", status="start", total=1),
            )
            r.on_event(
                _event("sketch", "done", fqn="seedbank.accession", elapsed_ms=5),
            )
            r.finish()

        lines = buf.getvalue().splitlines()
        tops = [i for i, line in enumerate(lines) if "╭" in line]
        assert len(tops) == 3
        assert "Cataloguing" in lines[tops[0] + 1]
        assert "Profiling" in lines[tops[1] + 1]
        assert "Sketching" in lines[tops[2] + 1]

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
    """The bar's remaining-time estimate, accumulated per segment as a running mean."""

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

            assert r._costs == {}
            assert "ETA --:--" in r._bar_line().plain

    def test_eta_is_remaining_times_the_observed_mean(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 21):
                r.on_event(_terminal_event("write", "done", i, 100, 5000))

            # 80 tables remain at an observed mean of 5s each.
            eta = _eta_seconds(r._costs, r._segment, *r._remaining_split())

        assert eta == pytest.approx(400.0)

    def test_a_run_of_skips_does_not_collapse_the_estimate(self) -> None:
        """The ETA estimate is not dominated by the most recent (all-skip) run."""

        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 21):
                r.on_event(_terminal_event("write", "done", i, 100, 5000))

            for i in range(21, 71):
                r.on_event(_terminal_event("extract", "skipped", i, 100, 10))

            eta = _eta_seconds(r._costs, r._segment, 100 - 70, 0)

            assert eta is not None
            assert eta > 20

    def test_a_failed_tables_duration_is_counted(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 21):
                r.on_event(_terminal_event("write", "done", i, 100, 5000))

            eta_before = _eta_seconds(r._costs, r._segment, 100 - 20, 0)
            r.on_event(_terminal_event("extract", "failed", 21, 100, 60_000))
            eta_after = _eta_seconds(r._costs, r._segment, 100 - 21, 0)

            assert eta_before is not None
            assert eta_after is not None
            assert eta_after > eta_before
            # It counts, and it counts as one sample among twenty-one - not as the whole estimate.
            assert eta_after < eta_before * 2

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

    def test_a_connections_own_start_resets_the_accumulator(self) -> None:
        """One renderer serves every connection in sequence - a prior connection's sketch-sized
        durations must not poison the next connection's first Profiling estimate.
        """

        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            # A sketch-sized duration, as the prior connection's Sketching pass would leave.
            r.on_event(_terminal_event("sketch", "done", 1, 1, 50_000))
            assert r._costs

            r.on_event(ProgressEvent(connection="second", phase="connecting", status="start"))

            assert r._costs == {}

            # The next connection's own (much smaller) table duration drives the ETA alone.
            r.on_event(_terminal_event("write", "done", 1, 2, 1_000))
            eta = _eta_seconds(r._costs, r._segment, 1, 0)

            assert eta == pytest.approx(1.0)

    def test_one_slow_table_never_ages_back_out_of_the_estimate(self) -> None:
        """An estimate that rises on a slow table stays risen while every table after it is fast -
        asserted on the per-unit cost, so only the slow table's tick may exceed 2x.
        """

        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)
        durations = [80] * 25 + [900_000] + [80] * 40
        unit_costs = []

        with LiveProgressRenderer(console) as r:
            for i, ms in enumerate(durations, start=1):
                r.on_event(_terminal_event("write", "done", i, len(durations), ms))
                unit_costs.append(_eta_seconds(r._costs, r._segment, 1, 0))

        steps = [max(a, b) / min(a, b) for a, b in pairwise(unit_costs) if a and b]

        assert len([s for s in steps if s > 2]) == 1

    def test_an_unentered_segment_is_priced_from_the_whole_run(self) -> None:
        """A segment with nothing observed borrows the run's mean rather than refusing to
        resolve, so a bar spanning several segments still carries a number.
        """

        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 5):
                r.on_event(_terminal_event("write", "done", i, 4, 4_000))

            eta = _eta_seconds(r._costs, "Sketching", 3, 0)

        assert eta == pytest.approx(12.0)

    def test_a_section_change_keeps_what_the_run_has_already_learned(self) -> None:
        """Each section keeps its own observed cost across a section change, so a section that
        has already been measured is never re-estimated from a single sample.
        """

        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 5):
                r.on_event(_terminal_event("write", "done", i, 4, 4_000))

            r.on_event(_terminal_event("sketch", "done", 1, 3, 20))

            assert _eta_seconds(r._costs, "Profiling", 1, 0) == pytest.approx(4.0)
            assert _eta_seconds(r._costs, "Sketching", 1, 0) == pytest.approx(0.02)


class TestBarPosition:
    """`_index`/`_total` describe where the run is - a bracket event must not overwrite them
    with a position the run never occupied.
    """

    def test_the_sketch_passs_own_closing_bracket_does_not_rewind_the_bar(self) -> None:
        """Drives the real `_ProgressEmitter`, the actual site the fix touches - a hand-built
        closing event could carry any `index` and would prove nothing about the emitter.
        """

        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            emitter = _ProgressEmitter(r.on_event, "acme")
            emitter.sketch_phase("start", 5)

            for i in range(1, 6):
                emitter.sketch_table("done", i, 5, f"s.t{i}", elapsed_ms=10)

            assert r._index == 5

            emitter.sketch_phase("done", 5)

            assert r._index == 5
            assert "0/5" not in r._bar_line().plain

    def test_finalizing_does_not_overwrite_the_last_real_phases_counters(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 6):
                r.on_event(_terminal_event("sketch", "done", i, 5, 10))

            assert r._index == 5
            assert r._total == 5

            r.on_event(
                ProgressEvent(connection="acme", phase="finalizing", status="start", total=8),
            )
            assert r._index == 5
            assert r._total == 5

            r.on_event(ProgressEvent(connection="acme", phase="finalizing", status="done", total=8))
            assert r._index == 5
            assert r._total == 5

    def test_a_second_connection_does_not_inherit_the_firsts_index(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 11):
                r.on_event(_terminal_event("write", "done", i, 10, 10))

            assert r._index == 10

            r.on_event(ProgressEvent(connection="secondary", phase="connecting", status="start"))
            assert r._index == 0
            assert r._total == 0

            r.on_event(
                ProgressEvent(connection="secondary", phase="listing", status="done", total=3),
            )
            assert r._index == 0
            assert r._total == 3
            assert "100%" not in r._bar_line().plain


class TestEtaDisplay:
    """The rendered ETA is quantised and held behind a deadband - `_eta_seconds` is untouched."""

    def test_the_60s_and_300s_tiers_quantise_to_their_own_step(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        under_hour = LiveProgressRenderer(console)._display_eta(1820.0)  # step 60s; -> 1800s
        over_hour = LiveProgressRenderer(console)._display_eta(4000.0)  # step 300s; -> 3900s

        assert under_hour == "0:30:00"
        assert over_hour == "1:05:00"

    def test_a_small_move_within_the_deadband_is_held(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)
        r = LiveProgressRenderer(console)

        first = r._display_eta(300.0)  # step 30s below 600s; quantises to 300s
        second = r._display_eta(310.0)  # diff 10s < 0.75 * 30s = 22.5s

        assert first == "0:05:00"
        assert second == "0:05:00"

    def test_a_move_past_the_deadband_updates(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)
        r = LiveProgressRenderer(console)

        first = r._display_eta(300.0)
        second = r._display_eta(330.0)  # diff 30s > 0.75 * 30s = 22.5s

        assert first == "0:05:00"
        assert second == "0:05:30"

    def test_an_order_of_magnitude_slowdown_moves_the_value_on_the_same_call(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)
        r = LiveProgressRenderer(console)

        first = r._display_eta(50.0)
        second = r._display_eta(500.0)

        assert first != second

    def test_a_raw_value_oscillating_near_a_tier_boundary_settles(self) -> None:
        """600s is the boundary between the 30s and 60s steps - a raw value hovering either
        side of it must not make the shown value alternate.
        """

        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)
        r = LiveProgressRenderer(console)

        shown = [r._display_eta(raw) for raw in (605.0, 595.0, 610.0, 590.0)]

        assert shown == ["0:10:00"] * 4

    def test_no_estimate_clears_the_shown_value(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)
        r = LiveProgressRenderer(console)

        r._display_eta(300.0)
        cleared = r._display_eta(None)

        assert cleared == "--:--"
        assert r._shown_eta is None

    def test_a_real_slowdown_moves_the_rendered_bar_within_one_tick(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 21):
                r.on_event(_terminal_event("write", "done", i, 100, 100))

            before = r._bar_line().plain

            r.on_event(_terminal_event("write", "done", 21, 100, 100_000))
            after = r._bar_line().plain

        assert before != after

    def test_a_second_connection_does_not_inherit_the_shown_value(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with LiveProgressRenderer(console) as r:
            for i in range(1, 6):
                r.on_event(_terminal_event("write", "done", i, 10, 5000))

            assert r._shown_eta is not None

            r.on_event(ProgressEvent(connection="secondary", phase="connecting", status="start"))

            assert r._shown_eta is None


class TestFinalFrameSpansTheRun:
    """`finish()`'s persistent frame after multiple connections - the whole run, not the last."""

    def test_two_connections_final_frame_sums_both(self) -> None:
        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with (
            patch.object(console, "print", wraps=console.print) as mock_print,
            LiveProgressRenderer(console) as r,
        ):
            for i in (1, 2):
                r.on_event(_terminal_event("write", "done", i, 2, 10))

            r.connection_summary(_result("primary", ok=2, failed=0, skipped=0, elapsed_ms=20))

            for i in (1, 2, 3):
                r.on_event(
                    ProgressEvent(
                        connection="secondary",
                        phase="write",
                        status="done",
                        index=i,
                        total=3,
                        fqn=f"s.t{i}",
                        elapsed_ms=10,
                    ),
                )

            r.connection_summary(_result("secondary", ok=3, failed=0, skipped=0, elapsed_ms=30))
            r.finish()

        final = str(mock_print.call_args_list[-1].args[0])

        # 2 tables from `primary` + 3 from `secondary` - not `secondary`'s own 3/3.
        assert "5/5" in final

    def test_the_sketch_pass_narrowing_index_total_does_not_shrink_the_final_frame(self) -> None:
        """`sketch` legitimately narrows `_index`/`_total` to its own sketchable subset - the
        final frame must span the connection's real table count, not whatever phase ran last.
        """

        console = Console(file=StringIO(), force_terminal=True, width=80, color_system=None)

        with (
            patch.object(console, "print", wraps=console.print) as mock_print,
            LiveProgressRenderer(console) as r,
        ):
            r.on_event(_terminal_event("write", "done", 100, 100, 10))
            r.on_event(ProgressEvent(connection="acme", phase="sketch", status="start", total=3))
            r.on_event(
                ProgressEvent(connection="acme", phase="sketch", status="done", index=3, total=3),
            )
            r.connection_summary(_result("acme", ok=100, failed=0, skipped=0, elapsed_ms=200))
            r.finish()

        final = str(mock_print.call_args_list[-1].args[0])

        assert "100/100" in final
        assert "3/3" not in final


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


class TestProgressIsOnStderr:
    """`generate`'s progress is on stderr, matching `check` and `diff` - stdout carries no
    payload of its own.
    """

    def test_progress_lines_are_on_stderr_not_stdout(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _MockPostgresAdapter},
            clear=True,
        ):
            result = CliRunner().invoke(main, ["generate", "--no-tui"])

        assert result.stdout == ""
        assert "public.a\tstart" in result.stderr
        assert "public.a\tok" in result.stderr

    def test_a_refused_tui_names_stderr(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _MockPostgresAdapter},
            clear=True,
        ):
            result = CliRunner().invoke(main, ["generate", "--tui"])

        assert "warning: --tui requested but stderr does not support the live view" in (
            result.stderr
        )


class TestRefusedTuiIsStated:
    """A dumb or too-short terminal refuses `--tui` too, not just an outright non-terminal -
    both routes downgrade through the same `supports_live` check.
    """

    def test_a_dumb_terminal_states_the_downgrade(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".dbprint.yaml").write_text(PROJECT_YAML)
        monkeypatch.chdir(tmp_path)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        # `TTY_COMPATIBLE=1` makes Rich report a real terminal without a pty; `TERM=dumb` then
        # makes it a dumb one, which `is_terminal` alone does not distinguish.
        monkeypatch.setenv("TTY_COMPATIBLE", "1")
        monkeypatch.setenv("TERM", "dumb")

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _MockPostgresAdapter},
            clear=True,
        ):
            result = CliRunner().invoke(main, ["generate", "--tui"])

        assert "warning: --tui requested but stderr does not support the live view" in (
            result.stderr
        )


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


class TestQuiet:
    """`-q`/`--quiet` silences generate's stderr progress; stdout carries no payload of its own."""

    @staticmethod
    def _invoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, *args: str):
        # A fresh project per call - two invocations sharing one tree would have the second
        # see the first's committed prints and skip re-profiling on freshness, not on quiet.
        project_root = tmp_path / name
        project_root.mkdir()
        (project_root / ".dbprint.yaml").write_text(PROJECT_YAML)
        monkeypatch.chdir(project_root)

        for k, v in _credential_env().items():
            monkeypatch.setenv(k, v)

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _MockPostgresAdapter},
            clear=True,
        ):
            return CliRunner().invoke(main, ["generate", *args])

    def test_quiet_leaves_stdout_empty(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._invoke(tmp_path, monkeypatch, "a", "--no-tui", "--quiet")

        assert result.stdout == ""

    def test_short_form_matches_the_long_one(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._invoke(tmp_path, monkeypatch, "a", "--no-tui", "-q")

        assert result.stdout == ""

    def test_exit_code_is_unaffected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        loud = self._invoke(tmp_path, monkeypatch, "loud", "--no-tui")
        quiet = self._invoke(tmp_path, monkeypatch, "quiet", "--no-tui", "--quiet")

        assert quiet.exit_code == loud.exit_code

    def test_quiet_with_tui_prints_no_refusal_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        result = self._invoke(tmp_path, monkeypatch, "a", "--quiet", "--tui")

        assert "does not support the live view" not in result.stderr

    def test_a_failure_still_reaches_stderr_under_quiet(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
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

        with patch.dict(
            "dbprint.cli.adapter_registry.ADAPTERS",
            {"postgres": _BrokenAdapter},
            clear=True,
        ):
            result = CliRunner().invoke(main, ["generate", "--no-tui", "--quiet"])

        assert result.stdout == ""
        assert "1 table failed: RuntimeError: boom" in result.stderr


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
