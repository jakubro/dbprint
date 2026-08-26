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
from collections import deque
from dataclasses import dataclass
from typing import Protocol, Self, TextIO

from rich.console import Console, Group
from rich.live import Live
from rich.text import Text

from dbprint.engine import (
    GenerateResult,
    ProgressEvent,
    SketchFailure,
    SummaryCounts,
    TableResult,
)
from dbprint.engine.result import ProgressStatus
from . import tree


_MIN_FOOTER_HEIGHT = 2
_BAR_WIDTH = 24
_REFRESH_PER_SECOND = 8
_ETA_WINDOW_DEFAULT = 20
# Every dbprint module logs through this name; see `install_log_handler` for why never root.
_DBPRINT_LOGGER_NAME = "dbprint"


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

    if live and _supports_live(console):
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

    Only a table's `start` and terminal events emit; statistics/write/finalizing do not.
    """

    def __init__(self, out: TextIO) -> None:
        self._out = out

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def on_event(self, event: ProgressEvent) -> None:
        if event.phase in ("connecting", "listing", "inventory"):
            self._write_prepass(event)

            return

        if event.phase == "finalizing" or event.fqn is None:
            return

        if event.phase == "extract" and event.status == "start":
            self._write(f"{event.connection}\t{event.fqn}\tstart")
        elif event.status == "done":
            self._write(
                f"{event.connection}\t{event.fqn}\tok\t"
                f"{tree.rows_text(event.row_count)}\t{tree.secs_text(event.elapsed_ms)}",
            )
        elif event.status == "failed":
            self._write(f"{event.connection}\t{event.fqn}\tfailed\t{event.error or ''}")
        elif event.status == "skipped":
            self._write(f"{event.connection}\t{event.fqn}\tskipped")

    def connection_summary(self, result: GenerateResult | ConnectionSummary) -> None:
        s = result.summary
        self._write(
            f"{result.connection_name}\tsummary\t"
            f"{s.ok} ok / {s.failed} failed / {s.skipped} skipped\t{result.elapsed_ms}ms",
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
        """One line per connection-wide phase event, with `inventory`'s count once known."""

        if event.phase == "inventory" and event.total:
            self._write(
                f"{event.connection}\tinventory\t{event.status}\t{event.index}/{event.total}",
            )
        else:
            self._write(f"{event.connection}\t{event.phase}\t{event.status}")

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
        self._phase_label = ""
        self._started = 0.0
        self._printed_path: tuple[str, ...] = ()
        self._any_failed = False
        self._finished = False
        # Per-state moving average (last N) feeding the ETA; `_terminal_counts` is unwindowed.
        self._durations: dict[ProgressStatus, deque[int]] = {
            "done": deque(maxlen=_ETA_WINDOW_DEFAULT),
            "skipped": deque(maxlen=_ETA_WINDOW_DEFAULT),
            "failed": deque(maxlen=_ETA_WINDOW_DEFAULT),
        }
        self._terminal_counts: dict[ProgressStatus, int] = {"done": 0, "skipped": 0, "failed": 0}
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
        self._connection = event.connection

        if event.phase in ("connecting", "listing", "inventory"):
            self._handle_prepass(event)
            self._live.update(self._footer())

            return

        self._index = event.index
        self._total = event.total

        if event.status in ("done", "failed", "skipped"):
            if event.fqn is not None:
                self._print_table_line(event)

            # `finalizing("done", ...)` reaches this branch too, with `fqn`/`elapsed_ms` None.
            if event.fqn is not None and event.elapsed_ms is not None:
                self._accumulate_duration(event.status, event.elapsed_ms)

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
        elif event.phase == "write":
            self._fqn = event.fqn or ""
            self._phase_label = "writing"
        else:
            self._fqn = event.fqn or ""
            self._phase_label = "extract ddl"

        self._live.update(self._footer())

    def _accumulate_duration(self, status: ProgressStatus, elapsed_ms: int) -> None:
        self._terminal_counts[status] += 1
        self._durations[status].append(elapsed_ms)

    def _handle_prepass(self, event: ProgressEvent) -> None:
        """Advance the in-flight label without moving the bar's own index.

        A connection-wide phase sets `total` once known but never `index`, so the bar does
        not appear to restart when the table loop's own events begin.
        """

        self._fqn = ""

        if event.total:
            self._total = event.total

        if event.phase == "connecting":
            self._phase_label = "connecting"
        elif event.phase == "listing":
            self._phase_label = "listing objects"
        else:
            self._phase_label = f"inventory  object {event.index}/{event.total}"

    def connection_summary(self, result: GenerateResult | ConnectionSummary) -> None:
        s = result.summary
        self._any_failed = (
            self._any_failed
            or s.failed > 0
            or result.error is not None
            or bool(result.sketch_failures)
        )
        line = (
            f"{result.connection_name}  -  {s.ok} ok  {s.failed} failed  "
            f"{s.skipped} skipped  -  {result.elapsed_ms}ms"
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
        label: str = "Profiling",
        style: str = "bold",
        show_eta: bool = True,
    ) -> Text:
        elapsed = _clock(time.monotonic() - self._started)
        pct = int(100 * self._index / self._total) if self._total else 0
        filled = int(_BAR_WIDTH * self._index / self._total) if self._total else 0
        bar = "#" * filled + "-" * (_BAR_WIDTH - filled)
        text = f"{label}  [{bar}]  {self._index}/{self._total}  {pct}%  {elapsed}"

        if show_eta:
            remaining = max(self._total - self._index, 0)
            eta = _eta_seconds(self._terminal_counts, self._durations, remaining)
            text += f"  ETA {'--:--' if eta is None else _clock(eta)}"

        return Text(text, style=style, no_wrap=True, overflow="ellipsis")

    def _inflight_line(self) -> Text:
        if not self._fqn and not self._phase_label:
            return Text("")

        target = self._fqn or self._connection
        text = f"  {target}   {self._phase_label}".rstrip()

        return Text(text, style="cyan", no_wrap=True, overflow="ellipsis")

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
        else:
            line = tree.leaf_metrics(
                leaf_depth,
                leaf,
                cap=cap,
                rows=tree.rows_text(event.row_count),
                elapsed=tree.secs_text(event.elapsed_ms),
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


def _supports_live(console: Console) -> bool:
    return (
        console.is_terminal
        and not console.is_dumb_terminal
        and console.size.height >= _MIN_FOOTER_HEIGHT
    )


def _clock(seconds: float) -> str:
    total = int(seconds)

    return f"{total // 3600}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _eta_seconds(
    counts: dict[ProgressStatus, int],
    durations: dict[ProgressStatus, deque[int]],
    remaining: int,
) -> float | None:
    """Share-weighted remaining-time estimate; None until one table reaches a terminal state.

    Each state's mean is a moving average over its own last-N window, weighted by that
    state's share of the whole run (`counts` is unwindowed).
    """

    total = sum(counts.values())

    if total == 0:
        return None

    weighted_ms = sum(
        (count / total) * (sum(durations[status]) / len(durations[status]))
        for status, count in counts.items()
        if count > 0
    )

    return max(remaining, 0) * weighted_ms / 1000


_TERMINAL_STYLE = {"done": "green", "failed": "red", "skipped": "yellow", "note": "dim"}
