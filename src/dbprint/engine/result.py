"""Request + result dataclasses crossing the CLI <-> Engine boundary.

All frozen; the results carry the typed outcomes rendering and exit-code derivation read.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal


TableStatus = Literal["ok", "skipped", "failed"]
ProgressPhase = Literal[
    "connecting",
    "listing",
    "inventory",
    "extract",
    "statistics",
    "write",
    "finalizing",
]
ProgressStatus = Literal["start", "done", "failed", "skipped"]


@dataclass(frozen=True)
class ProgressEvent:
    """One progress signal emitted while a generate run advances.

    Purely additive: emission never alters extraction, artifacts, or exit codes.
    `index`/`total` are scoped to the connection. `inventory` fires once per object in the
    relationship pre-pass, not per table re-profiled, so feeding it into the per-table tracker
    makes a wide connection's bar appear to restart. `fqn` is None for connection-wide phases.
    """

    connection: str
    phase: ProgressPhase
    status: ProgressStatus
    index: int = 0
    total: int = 0
    fqn: str | None = None
    column: str | None = None
    column_index: int | None = None
    column_total: int | None = None
    elapsed_ms: int | None = None
    row_count: int | None = None
    error: str | None = None


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass(frozen=True)
class GenerateRequest:
    """Per-call options for `Engine.generate()`.

    `on_progress` of None disables emission entirely - the strict no-op path `diff`/`check`
    rely on. `fail_fast` stops at the first table failure instead of isolating it.
    """

    force: bool = False
    dry_run: bool = False
    cli_include: tuple[str, ...] = ()
    cli_exclude: tuple[str, ...] = ()
    on_progress: ProgressCallback | None = None
    fail_fast: bool = False


@dataclass(frozen=True)
class DiffRequest:
    """Per-call options for `Engine.compute_diff()`, over extraction rather than a full run."""

    cli_include: tuple[str, ...] = ()
    cli_exclude: tuple[str, ...] = ()
    on_progress: ProgressCallback | None = None


@dataclass(frozen=True)
class TableResult:
    """Per-table outcome of a generate run.

    `error` is the one-line cause, formatted `<ExcType>: <message>`. `error_operation` names
    the adapter call that raised, since one message can come from more than one call;
    `error_detail` carries a query failure's statement and parameters.
    """

    fqn: str
    status: TableStatus
    error: str | None
    elapsed_ms: int
    error_operation: str | None = None
    error_detail: str | None = None
    error_traceback: str | None = None


@dataclass(frozen=True)
class SketchFailure:
    """One join-key column whose sketch query failed - the column carries no `sketch` key.

    Run-report-only (SPEC 7.1 has no way to say "attempted and errored" in the artifact);
    distinct from SPEC 7.2's four legitimate absence reasons, which this is not one of.
    """

    table: str
    column: str
    error: str


@dataclass(frozen=True)
class SummaryCounts:
    """Tally of per-table outcomes across one connection."""

    ok: int
    skipped: int
    failed: int


@dataclass(frozen=True)
class DiffSummary:
    """Mirrors SPEC 2.6.4 - exact event counts from the computed diff."""

    tables_added: int
    tables_removed: int
    tables_modified: int
    columns_added: int
    columns_removed: int
    columns_type_changed: int
    columns_nullable_changed: int
    columns_default_changed: int
    statistics_drifted: int
    relationships_changed: int
    indexes_changed: int
    comments_changed: int
    unchanged_tables: int
    unevaluated_tables: int


@dataclass(frozen=True)
class GenerateResult:
    """End-to-end result for one connection's generate run.

    `error` is the connection/config failure cause, None on success. `not_attempted` counts
    matched tables `--fail-fast` left unreached. `sketch_failures` names the join-key columns
    a query failure left unsketched; those columns generated normally otherwise.
    """

    connection_name: str
    tables: tuple[TableResult, ...]
    summary: SummaryCounts
    diff_summary: DiffSummary
    elapsed_ms: int
    exit_code: int
    error: str | None = None
    not_attempted: int = 0
    sketch_failures: tuple[SketchFailure, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class DiffResult:
    """End-to-end result for one connection's compute_diff run.

    `diff` is the full diff.yaml-shaped dict (SPEC 2.6), empty when no baseline exists or
    extraction never ran. `live_statistics` holds the just-extracted per-table stats that
    online assertions evaluate against instead of committed prints, empty on the same paths.
    """

    connection_name: str
    diff: dict[str, Any]
    target_scanned_tables: int
    elapsed_ms: int
    exit_code: int
    failed_tables: tuple[str, ...] = field(default_factory=tuple)
    live_statistics: dict[str, dict[str, Any]] = field(default_factory=dict)


# The whole exit-code vocabulary, including codes no engine method returns (`EXIT_ASSERTION`
# belongs to `check`), so every consumer of it reads one definition site.
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_STALENESS = 2
EXIT_DRIFT = 3
EXIT_CONNECTION = 4
EXIT_PARTIAL = 5
EXIT_ASSERTION = 6
EXIT_TOTAL_FAILURE = 7
