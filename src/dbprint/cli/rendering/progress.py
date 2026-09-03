"""Live + streaming progress rendering, shared by `dbprint generate` and `dbprint diff`.

Consumes engine `ProgressEvent`s. The live (TTY) renderer pins a two-line footer to the bottom
while table lines stream into scrollback above it as a connection/database/schema tree; the
streaming (non-TTY) renderer emits one plain line per table start/finish. Both end each
connection with the same summary line.
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass
from typing import Literal, Protocol, Self, TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from dbprint.conformance.progress import VALIDATION_PASSES
from dbprint.engine import (
    GenerateResult,
    ProgressEvent,
    SketchFailure,
    SummaryCounts,
    TableResult,
)
from . import tree


_MIN_FOOTER_HEIGHT = 2
_BAR_WIDTH = 24
_REFRESH_PER_SECOND = 8
# Every dbprint module logs through this name; see `install_log_handler` for why never root.
_DBPRINT_LOGGER_NAME = "dbprint"
# Fixes the bar's `[` column within the validate phase, whichever pass is running.
_VALIDATE_BAR_WIDTH = max(len(f"Validating {p}") for p in VALIDATION_PASSES)
# Display quantization for the ETA: (ceiling in seconds, step below it), finest near the end.
# A raw estimate at or above the last ceiling takes the final step.
_ETA_STEPS: tuple[tuple[float, float], ...] = ((60, 5), (600, 30), (3600, 60))
_ETA_STEP_DEFAULT = 300
# Fraction of the current step a raw estimate must clear before the shown value moves.
_ETA_DEADBAND = 0.75


@dataclass(frozen=True)
class ConnectionSummary:
    """Per-connection tally a progress renderer can summarize.

    `diff` builds one from its `DiffResult` so both commands share the summary line without
    coupling the two result dataclasses. `error` marks a connection that never reached a table,
    where `summary.failed` is 0 - the only signal `finish()` has that the run failed.
    """

    connection_name: str
    summary: SummaryCounts
    elapsed_ms: int
    tables: tuple[TableResult, ...] = ()
    error: str | None = None
    sketch_failures: tuple[SketchFailure, ...] = ()


class ProgressRenderer(Protocol):
    """Context-managed sink for `ProgressEvent`s plus per-connection summaries."""

    def __enter__(self) -> Self: ...

    def __exit__(self, *exc: object) -> None: ...

    def on_event(self, event: ProgressEvent) -> None: ...

    def connection_summary(self, result: GenerateResult | ConnectionSummary) -> None: ...

    def flush_warnings(self) -> None:
        """Flush whatever is held for the connection that just finished.

        Called once per connection unconditionally, including one that never reached
        `connection_summary()` (which flushes as part of its summary line), so no held
        warning survives into the next connection. A no-op for a renderer with nothing to hold.
        """
        ...

    def finish(self) -> None:
        """Own teardown: stop any live surface and print the run's final frame.

        Called once, after every connection's summary and before any other writer (a failure
        block, a held warning) is let out. A no-op for a renderer with no frame to finalize.
        """
        ...

    def log_record(self, text: str) -> None:
        """Route one dbprint log message; placement is the renderer's call."""
        ...


def build_progress_renderer(*, live: bool, console: Console, out: TextIO) -> ProgressRenderer:
    """Return the live footer when the console supports it, else plain streaming.

    Streaming covers not-a-TTY, a dumb terminal, and a terminal too short for the footer -
    a Rich `Live` written to a pipe garbles output.
    """

    if live and supports_live(console):
        return LiveProgressRenderer(console)

    return StreamingProgressRenderer(out)


class _RendererLogHandler(logging.Handler):
    """Forwards dbprint's own log records to the active renderer as bare message text.

    Pinned at WARNING regardless of the `dbprint` logger's own level, which the run log raises
    to DEBUG - the pin is what keeps engine detail off a user's terminal.
    """

    def __init__(self, renderer: ProgressRenderer) -> None:
        super().__init__(level=logging.WARNING)
        self._renderer = renderer
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._renderer.log_record(self.format(record))
        except Exception:  # noqa: BLE001 - logging.Handler.emit() contract: never raise
            self.handleError(record)


def install_log_handler(renderer: ProgressRenderer | None) -> logging.Handler:
    """Route dbprint's own log records to `renderer` for the life of one command.

    `renderer=None` is `--quiet`: a `logging.NullHandler`, so quiet is an explicit swallow
    rather than a fallback to `logging.lastResort`. Scoped to the `dbprint` logger, never root.
    Tear down with `remove_log_handler` in a `finally`.
    """

    handler: logging.Handler = (
        _RendererLogHandler(renderer) if renderer is not None else logging.NullHandler()
    )
    logging.getLogger(_DBPRINT_LOGGER_NAME).addHandler(handler)

    return handler


def remove_log_handler(handler: logging.Handler) -> None:
    """Undo `install_log_handler` - the counterpart every caller must run."""

    logging.getLogger(_DBPRINT_LOGGER_NAME).removeHandler(handler)


class StreamingProgressRenderer:
    """Plain one-line-per-table streaming for pipes / non-TTY / `--no-tui`.

    A table's `start` and terminal events emit; statistics/write/finalizing do not. The
    catalogue pre-pass emits one line per schema, the sketch pass one per table.
    """

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self._prepass_tracker = _PrepassSchemaTracker()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def on_event(self, event: ProgressEvent) -> None:
        if event.phase in ("connecting", "listing", "inventory"):
            self._write_prepass(event)

            return

        if event.phase == "sketch":
            self._write_sketch(event)

            return

        if event.phase in ("validate", "assertions"):
            self._write_validation(event)

            return

        if event.phase == "finalizing" or event.fqn is None:
            return

        if event.phase == "extract" and event.status == "start":
            self._write(f"{event.connection}\t{event.fqn}\tstart")
        elif event.status == "done":
            self._write(
                f"{event.connection}\t{event.fqn}\tok\t"
                f"{tree.rows_text(event.row_count)}\t{tree.duration_text(event.elapsed_ms)}",
            )
        elif event.status == "failed":
            self._write(f"{event.connection}\t{event.fqn}\tfailed\t{event.error or ''}")
        elif event.status == "skipped":
            self._write(f"{event.connection}\t{event.fqn}\tskipped")

    def connection_summary(self, result: GenerateResult | ConnectionSummary) -> None:
        s = result.summary
        self._write(
            f"{result.connection_name}\tsummary\t"
            f"{s.ok} ok / {s.failed} failed / {s.skipped} skipped\t"
            f"{tree.duration_text(result.elapsed_ms)}",
        )

        for sketch_failure in result.sketch_failures:
            self._write(
                f"{result.connection_name}\t{sketch_failure.table}.{sketch_failure.column}\t"
                f"sketch_failed\t{sketch_failure.error}",
            )

    def flush_warnings(self) -> None:
        """Nothing to flush - `log_record` writes straight to stderr, unqueued."""

        return

    def finish(self) -> None:
        """No frame to finalize - the streaming renderer has none to begin with."""

        return

    def log_record(self, text: str) -> None:
        """One plain line to stderr; `self._out` under `generate` is the stdout data stream."""

        print(text, file=sys.stderr, flush=True)

    def _write_prepass(self, event: ProgressEvent) -> None:
        """Bracket lines for `connecting`/`listing`/`inventory`; `inventory` also closes a
        schema line on each schema change and on the phase's own "done".
        """

        if event.phase != "inventory":
            self._write(f"{event.connection}\t{event.phase}\t{event.status}")

            return

        if event.fqn is None:
            self._write(f"{event.connection}\tinventory\t{event.status}")

            if event.status == "done":
                self._write_prepass_schema(event.connection, self._prepass_tracker.close())

            return

        self._write_prepass_schema(event.connection, self._prepass_tracker.tick(event.fqn))

    def _write_prepass_schema(self, connection: str, closed: _ClosedSchema | None) -> None:
        if closed is None:
            return

        self._write(
            f"{connection}\tinventory\tschema\t{closed.schema_key}\t"
            f"{tree.objects_text(closed.count)}\t{tree.duration_text(closed.elapsed_ms)}",
        )

    def _write_validation(self, event: ProgressEvent) -> None:
        """One line per table: `validate` ticks once per pass and only the closing tick carries
        a finding count, matching the live renderer; `assertions` fans out no passes.
        """

        if event.fqn is None:
            return

        if event.phase == "validate":
            if event.findings is None:
                return

            self._write(
                f"{event.connection}\t{event.fqn}\tvalidated\t"
                f"{tree.findings_text(event.findings)}\t{event.severity or 'clean'}",
            )
        else:
            self._write(
                f"{event.connection}\t{event.fqn}\tasserted\t"
                f"{tree.duration_text(event.elapsed_ms)}",
            )

    def _write_sketch(self, event: ProgressEvent) -> None:
        """One line per table, on its terminal event only - column ticks stay silent."""

        if event.fqn is None or event.status not in ("done", "failed"):
            return

        if event.status == "done":
            self._write(
                f"{event.connection}\t{event.fqn}\tsketched\t{tree.duration_text(event.elapsed_ms)}",
            )
        else:
            self._write(f"{event.connection}\t{event.fqn}\tsketch_failed\t{event.error or ''}")

    def _write(self, line: str) -> None:
        self._out.write(line + "\n")
        self._out.flush()


class LiveProgressRenderer:
    """Bounded two-line footer (overall bar + in-flight table) over scrollback.

    Terminal table lines print into normal scrollback above a footer of constant height; the
    alternate screen is never entered. The footer is transient, so exiting the context
    manager (including via SIGINT) clears it and restores the cursor.
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._live = Live(
            console=console,
            refresh_per_second=_REFRESH_PER_SECOND,
            transient=True,
            auto_refresh=True,
        )
        self._connection = ""
        self._index = 0
        self._total = 0
        self._fqn = ""
        # The in-flight line's own target - separate from `_fqn` so the catalogue pass can name
        # its schema without `_fqn` (the warning-routing switch) believing a table is in flight.
        self._display = ""
        self._phase_label = ""
        # Empty until the first event names a phase - never a guess at one that hasn't begun.
        self._bar_label = ""
        self._started = 0.0
        self._printed_path: tuple[str, ...] = ()
        # Sections that already drew their box this connection - see `_begin_section`.
        self._headed_sections: set[str] = set()
        self._any_failed = False
        self._finished = False
        # Run-wide counters `finish()`'s persistent frame reads instead of `_index`/`_total`,
        # which `on_event` resets to each connection's own span.
        self._run_index = 0
        self._run_total = 0
        # Observed cost per segment, feeding the ETA. A running mean, never a window: an outlier
        # that raises the estimate must not lower it again by ageing out of a fixed sample count.
        self._costs: dict[str, _Cost] = {}
        # The ETA last shown, held behind a deadband so a value re-estimated every tick does
        # not redraw every tick - see `_display_eta`. None until the first estimate resolves.
        self._shown_eta: float | None = None
        # What the bar counts right now, and the index at which it stops counting it. `None` spans
        # the rest of the bar - only `check`'s validation bar holds more than one segment.
        self._segment = ""
        self._segment_end: int | None = None
        # An inventory event carries no `elapsed_ms`, so a per-object duration is the delta
        # between ticks - the same technique `_validation_progress_adapter` uses.
        self._prepass_last_tick: float | None = None
        self._prepass_tracker = _PrepassSchemaTracker()
        # A warning raised while `_fqn` names a table waits for that table's leaf line;
        # one raised with no table in flight waits for the connection summary.
        self._pending_table_warnings: list[str] = []
        self._held_warnings: list[str] = []

    def __enter__(self) -> Self:
        self._started = time.monotonic()
        self._live.start()

        return self

    def __exit__(self, *exc: object) -> None:
        # Interrupt path only - a normal run already called finish(); transient cleanup only.
        if not self._finished:
            self._live.stop()

    def on_event(self, event: ProgressEvent) -> None:
        if event.connection != self._connection:
            # One shared renderer serves every connection in sequence - a repeated label
            # re-heads, and the counters reset since the prior span already reached `_run_index`.
            # The ETA resets here too: every command passes through this branch on a connection
            # change, where `connecting` alone does not - `check` never emits it.
            self._headed_sections = set()
            self._index = 0
            self._total = 0
            self._reset_connection_eta()

        self._connection = event.connection

        if event.phase in ("connecting", "listing", "inventory"):
            self._handle_prepass(event)
            self._live.update(self._footer())

            return

        if event.phase in ("validate", "assertions"):
            self._handle_validation(event)
            self._live.update(self._footer())

            return

        self._display = ""

        # `finalizing` carries the table count under whichever phase preceded it - a bracket,
        # not a position - so the bar keeps the last real phase's counters through it.
        if event.phase != "finalizing":
            self._index = event.index
            self._total = event.total

        if event.phase == "sketch":
            self._begin_section("Sketching")
            self._bar_label = "Sketching"
            self._enter_segment("Sketching")
        elif event.phase != "finalizing":
            self._begin_section("Profiling")
            self._bar_label = "Profiling"
            self._enter_segment("Profiling")

        if event.status in ("done", "failed", "skipped"):
            if event.fqn is not None:
                self._print_table_line(event)

            # `finalizing("done", ...)` reaches this branch too, with `fqn`/`elapsed_ms` None.
            if event.fqn is not None and event.elapsed_ms is not None:
                self._accumulate_duration(event.elapsed_ms)

            self._fqn = ""
            self._phase_label = ""
        elif event.phase == "finalizing":
            self._fqn = ""
            self._phase_label = "finalizing"
        elif event.phase == "statistics" and event.column is not None:
            self._fqn = event.fqn or ""
            self._phase_label = (
                f"statistics  column {event.column_index}/{event.column_total} ({event.column})"
            )
        elif event.phase == "sketch" and event.column is not None:
            self._fqn = event.fqn or ""
            self._phase_label = f"column {event.column_index}/{event.column_total} ({event.column})"
        elif event.phase == "write":
            self._fqn = event.fqn or ""
            self._phase_label = "writing"
        elif event.phase == "sketch":
            self._fqn = event.fqn or ""
            self._phase_label = ""
        else:
            self._fqn = event.fqn or ""
            self._phase_label = "extract ddl"

        self._live.update(self._footer())

    def _reset_connection_eta(self) -> None:
        """Drop every observed cost and the shown value they produced. A different connection
        is different work; a section change within one connection keeps what it has learned.
        """

        self._costs = {}
        self._shown_eta = None

    def _accumulate_duration(self, elapsed_ms: float) -> None:
        cost = self._costs.setdefault(self._segment, _Cost())
        cost.total_ms += elapsed_ms
        cost.count += 1

    def _enter_segment(self, segment: str, *, ends_at: int | None = None) -> None:
        """Name the run of work the bar is counting, and the bar index where it stops - `ends_at`
        of None spans the rest of the bar, only `check`'s per-pass ETA needing narrower.
        """

        self._segment = segment
        self._segment_end = ends_at

    def _begin_section(self, section: str) -> None:
        """Draw `section`'s boxed header once per connection, on section transition rather than
        on bar-label change - the label repeats across connections and across a pass's ticks.
        """

        if section in self._headed_sections:
            return

        self._headed_sections.add(section)
        self._printed_path = ()
        cap = tree.resolve_cap(self._console.width)
        self._console.print(Text(tree.banner_box(section, cap=cap), style="bold"))

    def _handle_prepass(self, event: ProgressEvent) -> None:
        """`connecting`/`listing` only set the label and, once known, `total` - the bar isn't
        real yet, so `index` never moves. `inventory` drives its own `Cataloguing` bar and
        closes a scrollback row on each schema change.
        """

        self._fqn = ""
        self._display = ""

        if event.phase == "connecting":
            if event.total:
                self._total = event.total

            self._bar_label = "Connecting"
            self._enter_segment("Connecting")
            # No target yet - the bar label alone says which phase this is.
            self._phase_label = ""

            return

        if event.phase == "listing":
            if event.total:
                self._total = event.total

            self._bar_label = "Listing objects"
            self._enter_segment("Listing objects")
            self._phase_label = ""

            return

        # inventory: `fqn` is None for the phase's own start/done bracket, set on each tick.
        if event.fqn is None:
            if event.status == "start":
                self._begin_section("Cataloguing")
                self._bar_label = "Cataloguing"
                self._enter_segment("Cataloguing")
                self._index = 0
                self._total = event.total
                self._prepass_last_tick = time.monotonic()
            else:
                self._print_prepass_schema(self._prepass_tracker.close())

            return

        self._print_prepass_schema(self._prepass_tracker.tick(event.fqn))

        if self._prepass_last_tick is not None:
            now = time.monotonic()
            self._accumulate_duration((now - self._prepass_last_tick) * 1000)
            self._prepass_last_tick = now

        self._index = event.index
        self._total = event.total
        self._display = self._prepass_tracker.current or ""
        # Line two names the target only - `_display` already is the schema being counted.
        self._phase_label = ""

    def _handle_validation(self, event: ProgressEvent) -> None:
        """`check`'s offline passes: one bar spanning every one, named by `event.pass_name`.

        Two shapes share it: `validate` advances on every tick but prints a leaf only on the one
        carrying `findings`; `assertions` prints a row/duration leaf on every tick.
        """

        label = "Validating" if event.phase == "validate" else "Checking assertions"
        self._begin_section(label)

        if event.phase == "validate":
            self._bar_label = f"Validating {event.pass_name}".ljust(_VALIDATE_BAR_WIDTH)
            self._enter_segment(f"Validating {event.pass_name}", ends_at=_pass_end(event))
        else:
            self._bar_label = label
            self._enter_segment(label)

        self._index = event.index
        self._total = event.total
        self._display = ""
        self._fqn = event.fqn or ""
        # Line two names the target only - the running pass lives in the bar label.
        self._phase_label = ""

        # Every tick carries its own `elapsed_ms` (`_validation_progress_adapter` times the delta
        # since the previous one), so the ETA accumulates whether or not this tick closes a table.
        if event.elapsed_ms is not None:
            self._accumulate_duration(event.elapsed_ms)

        if event.phase == "assertions":
            if event.fqn is not None:
                self._print_table_line(event)

            self._fqn = ""
            self._phase_label = ""

            return

        findings = event.findings

        if findings is None:
            return

        if event.fqn is not None:
            self._print_validation_line(event.fqn, findings, event.severity)

        self._fqn = ""
        self._phase_label = ""

    def _print_prepass_schema(self, closed: _ClosedSchema | None) -> None:
        if closed is None:
            return

        cap = tree.resolve_cap(self._console.width)
        path = tree.header_path(self._connection, closed.schema_key)

        for depth, name in tree.divergent_headers(self._printed_path, path):
            self._console.print(Text(tree.header_line(depth, name, cap=cap), style="bold"))

        self._printed_path = path
        line = tree.leaf_metrics(
            len(path),
            tree.leaf_name(closed.schema_key),
            cap=cap,
            rows=tree.objects_text(closed.count),
            elapsed=tree.duration_text(closed.elapsed_ms),
        )
        self._console.print(Text(line, style=_TERMINAL_STYLE["done"]))

    def connection_summary(self, result: GenerateResult | ConnectionSummary) -> None:
        s = result.summary
        self._any_failed = (
            self._any_failed
            or s.failed > 0
            or result.error is not None
            or bool(result.sketch_failures)
        )
        # `_index`/`_total` hold whichever phase ran last for this connection, so the
        # `s.ok`/`s.failed`/`s.skipped` tally is the authoritative per-table count.
        table_count = s.ok + s.failed + s.skipped
        self._run_index += table_count
        self._run_total += table_count
        line = (
            f"{result.connection_name}  -  {s.ok} ok  {s.failed} failed  "
            f"{s.skipped} skipped  -  {tree.duration_text(result.elapsed_ms)}"
        )
        self._console.print(Text(line, style="bold"))

        for tbl in result.tables:
            if tbl.status == "failed":
                self._console.print(Text(f"  failed: {tbl.fqn}", style="red"))

        for sketch_failure in result.sketch_failures:
            self._console.print(
                Text(
                    f"  sketch failed: {sketch_failure.table}.{sketch_failure.column}",
                    style="red",
                ),
            )

        self._flush_held_warnings()

    def flush_warnings(self) -> None:
        """No-op unless a caller's control flow skipped `connection_summary()`."""

        self._flush_held_warnings()

    def log_record(self, text: str) -> None:
        """Attach to the in-flight table if there is one, else hold for the summary."""

        if self._fqn:
            fragment = f" for {self._fqn!r}"

            if fragment in text:
                text = text.replace(fragment, "", 1)

            self._pending_table_warnings.append(text)
        else:
            self._held_warnings.append(text)

    def finish(self) -> None:
        """Stop the live and print a persistent final frame carrying the verdict.

        The bar survives with its label switched to `Completed` and no ETA, printed as
        ordinary output so it stays in scrollback after the live region stops.
        """

        if self._finished:
            return

        self._finished = True
        self._live.stop()
        # A warning held for a table whose terminal event never fired must still be printed.
        self._flush_held_warnings()
        # The live footer is done updating, so this frame is the one place `_index`/`_total`
        # may hold the run-wide sum rather than the last connection's own span.
        self._index = self._run_index
        self._total = self._run_total
        label = "Completed with failures" if self._any_failed else "Completed"
        style = "red" if self._any_failed else "bold"
        self._console.print(self._bar_line(label=label, style=style, show_eta=False))

    def _flush_held_warnings(self) -> None:
        cap = tree.resolve_cap(self._console.width)

        for text in (*self._held_warnings, *self._pending_table_warnings):
            self._console.print(
                Text(tree.warning_line(0, text, cap=cap), style=_TERMINAL_STYLE["note"]),
            )

        self._held_warnings = []
        self._pending_table_warnings = []

    def _footer(self) -> Group:
        return Group(self._bar_line(), self._inflight_line())

    def _bar_line(
        self,
        *,
        label: str | None = None,
        style: str = "bold",
        show_eta: bool = True,
    ) -> Text:
        label = self._bar_label if label is None else label
        elapsed = _clock(time.monotonic() - self._started)
        pct = int(100 * self._index / self._total) if self._total else 0
        filled = int(_BAR_WIDTH * self._index / self._total) if self._total else 0
        bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
        text = f"{label}  [{bar}]  {self._index}/{self._total}  {pct}%  {elapsed}"

        if show_eta:
            in_segment, beyond = self._remaining_split()
            eta = _eta_seconds(self._costs, self._segment, in_segment, beyond)
            text += f"  ETA {self._display_eta(eta)}"

        return Text(text, style=style, no_wrap=True, overflow="ellipsis")

    def _display_eta(self, raw: float | None) -> str:
        """Render `raw` seconds through the shown-value deadband - the value holds until `raw`
        clears `_ETA_DEADBAND`, so re-estimating every tick does not redraw every tick.
        """

        if raw is None:
            self._shown_eta = None

            return "--:--"

        step = _eta_step(raw)

        if self._shown_eta is None or abs(raw - self._shown_eta) > _ETA_DEADBAND * step:
            self._shown_eta = round(raw / step) * step

        return _clock(self._shown_eta)

    def _remaining_split(self) -> tuple[int, int]:
        """Bar units left in the running segment, and units left in segments not yet entered."""

        remaining = max(self._total - self._index, 0)

        if self._segment_end is None:
            return remaining, 0

        in_segment = max(min(self._segment_end, self._total) - self._index, 0)

        return in_segment, remaining - in_segment

    def _inflight_line(self) -> Text:
        if not self._display and not self._fqn and not self._phase_label:
            return Text("")

        target = self._display or self._fqn or self._connection
        text = f"  {target}   {self._phase_label}".rstrip()

        return Text(text, style="cyan", no_wrap=True, overflow="ellipsis")

    def _print_validation_line(
        self,
        fqn: str,
        findings: int,
        severity: Literal["error", "warning"] | None,
    ) -> None:
        cap = tree.resolve_cap(self._console.width)
        path = tree.header_path(self._connection, fqn)

        for depth, name in tree.divergent_headers(self._printed_path, path):
            self._console.print(Text(tree.header_line(depth, name, cap=cap), style="bold"))

        self._printed_path = path
        leaf_depth = len(path)
        leaf = tree.leaf_name(fqn)
        line = tree.leaf_findings(leaf_depth, leaf, cap=cap, findings=findings)
        self._console.print(
            Text(line, style=_SEVERITY_STYLE[severity], no_wrap=True, overflow="ellipsis"),
        )
        self._flush_table_warnings(leaf_depth, cap=cap)

    def _print_table_line(self, event: ProgressEvent) -> None:
        fqn = event.fqn or ""
        cap = tree.resolve_cap(self._console.width)
        path = tree.header_path(event.connection, fqn)

        for depth, name in tree.divergent_headers(self._printed_path, path):
            self._console.print(Text(tree.header_line(depth, name, cap=cap), style="bold"))

        self._printed_path = path
        leaf_depth = len(path)
        leaf = tree.leaf_name(fqn)

        if event.status == "failed":
            line = tree.leaf_error(leaf_depth, leaf, event.error or "", cap=cap)
        elif event.status == "skipped":
            line = tree.leaf_note(leaf_depth, leaf, "(skipped)", cap=cap)
        elif event.phase in ("sketch", "assertions"):
            # Neither phase reads table rows - a borrowed `- rows` would claim a measurement
            # this tick never took.
            line = tree.leaf_duration(
                leaf_depth,
                leaf,
                cap=cap,
                elapsed=tree.duration_text(event.elapsed_ms),
            )
        else:
            line = tree.leaf_metrics(
                leaf_depth,
                leaf,
                cap=cap,
                rows=tree.rows_text(event.row_count),
                elapsed=tree.duration_text(event.elapsed_ms),
            )

        self._console.print(
            Text(line, style=_TERMINAL_STYLE[event.status], no_wrap=True, overflow="ellipsis"),
        )
        self._flush_table_warnings(leaf_depth, cap=cap)

    def _flush_table_warnings(self, leaf_depth: int, *, cap: int) -> None:
        for text in self._pending_table_warnings:
            self._console.print(
                Text(tree.warning_line(leaf_depth, text, cap=cap), style=_TERMINAL_STYLE["note"]),
            )

        self._pending_table_warnings = []


def supports_live(console: Console) -> bool:
    """Whether `console` can host a live-redrawn footer: a real terminal, not dumb, tall enough -
    the same check a --tui-refused warning reads, so every downgrade reason is stated.
    """

    return (
        console.is_terminal
        and not console.is_dumb_terminal
        and console.size.height >= _MIN_FOOTER_HEIGHT
    )


def _clock(seconds: float) -> str:
    total = int(seconds)

    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _eta_step(seconds: float) -> float:
    """Display quantization step for a raw ETA - finer near the end, coarser far from it."""

    for ceiling, step in _ETA_STEPS:
        if seconds < ceiling:
            return step

    return _ETA_STEP_DEFAULT


@dataclass
class _Cost:
    """Every duration one segment has been observed to take, as a running total and a count."""

    total_ms: float = 0.0
    count: int = 0


def _pass_end(event: ProgressEvent) -> int | None:
    """Bar index where the running pass stops, or None with no ordinals - same object count
    per pass divides evenly.
    """

    if event.pass_index is None or not event.pass_total:
        return None

    return event.pass_index * (event.total // event.pass_total)


def _mean_ms(costs: dict[str, _Cost], segment: str | None) -> float | None:
    """Mean observed cost of one segment, or of the whole run when `segment` is None - pooled
    across terminal states, a skip costing nothing being a real part of the next unit's cost.
    """

    selected = [c for key, c in costs.items() if segment is None or key == segment]
    count = sum(c.count for c in selected)

    if count == 0:
        return None

    return sum(c.total_ms for c in selected) / count


def _eta_seconds(
    costs: dict[str, _Cost],
    segment: str,
    in_segment: int,
    beyond: int,
) -> float | None:
    """Remaining-time estimate; None until some segment has observed one unit of work - priced
    at the running segment's mean, then the run's mean beyond it.
    """

    run_mean = _mean_ms(costs, None)

    if run_mean is None:
        return None

    segment_mean = _mean_ms(costs, segment)

    if segment_mean is None:
        segment_mean = run_mean

    return (max(in_segment, 0) * segment_mean + max(beyond, 0) * run_mean) / 1000


@dataclass(frozen=True)
class _ClosedSchema:
    schema_key: str
    count: int
    elapsed_ms: int


@dataclass
class _PrepassSchemaTracker:
    """A tick fires before the object is read, so the schema derived from fqn changes one
    object late; closing on the change keeps count and elapsed attached to the right schema.
    """

    current: str | None = None
    count: int = 0
    started: float = 0.0

    def tick(self, fqn: str) -> _ClosedSchema | None:
        """Returns the previous schema's tally when `fqn` starts a new one."""

        schema_key = fqn.rsplit(".", 1)[0] if "." in fqn else fqn
        closed = None

        if schema_key != self.current:
            closed = self.close()
            self.current = schema_key
            self.started = time.monotonic()

        self.count += 1

        return closed

    def close(self) -> _ClosedSchema | None:
        """Return the open schema's tally and clear it; None when nothing is open."""

        if self.current is None:
            return None

        closed = _ClosedSchema(
            schema_key=self.current,
            count=self.count,
            elapsed_ms=int((time.monotonic() - self.started) * 1000),
        )
        self.current = None
        self.count = 0

        return closed


_TERMINAL_STYLE = {"done": "green", "failed": "red", "skipped": "yellow", "note": "dim"}
# A validated table's own leaf colour: red where it holds an error, yellow where it holds
# only warnings, green where clean - severity the tick carries, not the table's own status.
_SEVERITY_STYLE: dict[Literal["error", "warning"] | None, str] = {
    "error": "red",
    "warning": "yellow",
    None: "green",
}
