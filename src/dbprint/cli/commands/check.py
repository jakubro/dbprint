"""`dbprint check` - offline + --online CI gate over committed prints.

The online phase runs only when the offline half found the print fit to compare: not a
conformance error, not stale. Top-level exit code = max across every evaluated check
(ASSERTIONS.md 6.3).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import rich_click as click
import yaml
from rich.console import Console

from dbprint.adapters import trace_context
from dbprint.assertions import (
    AssertionSet,
    ParseError,
    ParseFault,
    evaluate_sql_assertions,
    evaluate_statistic_assertions,
    parse_block,
)
from dbprint.config import ConfigError, ConnectionConfig
from dbprint.conformance import Issue, ValidationProgress, ValidationTick, validate_print
from dbprint.engine import (
    EXIT_ASSERTION,
    EXIT_CONNECTION,
    EXIT_DRIFT,
    EXIT_GENERIC,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_STALENESS,
    DiffRequest,
    ProgressEvent,
    SummaryCounts,
    TableResult,
)
from dbprint.engine.baseline import declared_artifacts, manifest_shape_error, walkable_tables
from dbprint.engine.diff import DATA_CHANGE_KINDS
from dbprint.engine.freshness import DurationError, evaluate, parse_duration
from dbprint.engine.result import DiffResult
from .. import thresholds
from ..engine_setup import ConnectionSetupError, build_engine
from ..options import project_option, refuse_if_remote, resolve_project
from ..rendering import (
    build_progress_renderer,
    install_log_handler,
    remove_log_handler,
    resolve_render_mode,
    supports_live,
)
from ..rendering.check_data import CheckResult, NotRun, OnlineDisposition, render_data
from ..rendering.check_tty import render_human
from ..rendering.errors import emit_error
from ..rendering.progress import ConnectionSummary, ProgressRenderer
from ..resolution import ConnectionResolutionError, resolve
from ..run_log import close_run_log, log_run_header, log_run_summary, open_run_log


@click.command(name="check")
@click.argument("conn", required=False)
@project_option
@click.option(
    "--max-age",
    "max_age",
    type=str,
    default=None,
    help="Max staleness before a print is stale (exit 2), applied to every table. Duration "
    "`Nd/Nh/Nm/Ns` - e.g. 7d, 12h, 30m; no compound forms like 1d12h. Default: the "
    "threshold each table's own print records, falling back to what its `rules` resolve "
    "to for a print that records none - offline, only rules matching by name apply.",
)
@click.option(
    "--online",
    "online",
    is_flag=True,
    default=False,
    help="Verify against the live database: schema and statistics drift + SQL assertions.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["human", "json", "yaml"], case_sensitive=False),
    default="human",
    show_default=True,
    help="Output format.",
)
@click.option(
    "--tui/--no-tui",
    default=None,
    help="Force TTY (Rich) or piped (plain-text) progress rendering, on stderr.",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    default=False,
    help="Silence stderr progress (footer / tree / streaming / summary); stdout payload unaffected.",
)
@click.pass_context
def check_command(
    ctx: click.Context,
    conn: str | None,
    project: str | None,
    max_age: str | None,
    online: bool,
    fmt: str,
    tui: bool | None,
    quiet: bool,
) -> None:
    """Verify committed prints are well-formed, fresh, and meet assertions.

    CI gate over the committed prints.

    - **Offline** (default): the manifest is present and conformance-valid,
      every print is within `--max-age`, and statistic assertions hold.
    - **Online** (`--online`): additionally re-extracts the live database to
      detect drift - both a change of shape and a moved statistic - and
      evaluate SQL assertions.

    The reported exit code is the worst across every evaluated check.
    `--online` writes one run log to `~/.dbprint/logs/<project-slug>/`, keeping
    the 3 most recent; the offline default writes nothing.

    **Arguments:**

    - `CONN`: connection to check; resolved from `.dbprint.yaml` when omitted
      (the `auto: true` set, or the sole connection).

    **Exit codes:**

    - `0`: ok
    - `1`: generic - a malformed print, or a table whose `rules` narrow it both
      by a predicate and by a fraction, which this command refuses to judge
    - `2`: staleness
    - `3`: drift (`--online`) - the committed print no longer matches the
      database, including a statistic that moved (`generate` sets this code for
      a change of shape only)
    - `4`: connection (`--online`, the database could not be reached)
    - `5`: partial extraction (`--online`) - the connection was reached but some
      tables could not be re-extracted; the ones that did are still compared
      and reported normally
    - `6`: assertion failure

    **Examples:**

    - `dbprint check`: offline CI gate (no credentials)
    - `dbprint check --max-age 24h`: fail if any print is older than a day
    - `dbprint check --online`: add live drift + SQL assertions
    """

    if online:
        refuse_if_remote(project, "check --online")

    project_config = resolve_project(project)

    try:
        connections = resolve(project_config, conn)
    except ConnectionResolutionError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    run_log = open_run_log(project_config.project_root, "check") if online else None

    # Progress goes to stderr so stdout stays a clean result envelope; TTY-detect follows suit.
    err_console = Console(stderr=True)
    mode = resolve_render_mode(tui, err_console)

    if not quiet and tui is True and not supports_live(err_console):
        click.echo(
            "warning: --tui requested but stderr does not support the live view; "
            "using plain output",
            err=True,
        )

    renderer = (
        None
        if quiet
        else build_progress_renderer(
            live=mode == "tty",
            console=err_console,
            out=click.get_text_stream("stderr"),
        )
    )
    # A renderer always exists (or is explicitly None under --quiet), and
    # `install_log_handler` twice on the same logger would print every warning twice.
    log_handler = install_log_handler(renderer)

    # Stderr causes are deferred until the live footer stops and prints its final frame -
    # nothing else may share the screen while it is up. Order is preserved.
    deferred: list[str] = []

    try:
        if online:
            log_run_header(project_config.project_root, [c.name for c in connections])

        results: list[CheckResult] = []

        with renderer if renderer is not None else contextlib.nullcontext():
            for conn_config in connections:
                try:
                    results.append(
                        _check_one(
                            conn_config,
                            max_age,
                            online,
                            project_config.project_root,
                            ctx,
                            renderer=renderer,
                            deferred=deferred,
                        ),
                    )
                finally:
                    if renderer is not None:
                        renderer.flush_warnings()

            if renderer is not None:
                renderer.finish()

        for text in deferred:
            emit_error(text)

        stream = click.get_text_stream("stdout")
        fmt_lower = fmt.lower()

        if fmt_lower in {"json", "yaml"}:
            render_data(results, fmt_lower, stream)
        else:
            render_human(results, stream)

        top = max((r.exit_code for r in results), default=EXIT_OK)

        if online:
            log_run_summary(top)

        ctx.exit(top)
    finally:
        remove_log_handler(log_handler)
        close_run_log(run_log)


def _check_one(
    conn_config: ConnectionConfig,
    max_age_arg: str | None,
    online: bool,
    project_root: Path,
    ctx: click.Context,
    *,
    renderer: ProgressRenderer | None,
    deferred: list[str],
) -> CheckResult:
    """Run offline checks + optional online checks for one connection."""

    started = time.monotonic()

    try:
        override = parse_duration(max_age_arg) if max_age_arg else None
    except DurationError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    default_max_age_days = override if override is not None else conn_config.max_age_days

    print_root = _print_root(conn_config)
    manifest_present = (print_root / "manifest.yaml").is_file()

    if not manifest_present:
        result = CheckResult(
            connection_name=conn_config.name,
            print_root=str(print_root),
            manifest_present=False,
            issues=(),
            stale_entries=(),
            default_max_age_days=float(default_max_age_days),
            exit_code=EXIT_GENERIC,
        )

        if renderer is not None:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            renderer.connection_summary(_summary_view(result, None, elapsed_ms))

        return result

    issues = tuple(
        validate_print(
            print_root,
            on_table=_validation_progress_adapter(renderer, conn_config.name),
        ),
    )
    has_errors = any(i.severity == "error" for i in issues)

    manifest = _load_manifest(print_root)

    # Resolved regardless of --max-age: a table narrowed two ways is a scope error
    # independent of freshness; the override only changes what the refusal costs.
    resolved = thresholds.resolve(conn_config, manifest)
    not_run = _not_run_from(resolved, exit_moving=override is None)

    # Under an override no rule supplies the threshold, so the size-gate warning would be false.
    if override is None and resolved.size_gated:
        deferred.append(thresholds.size_gate_warning(conn_config.name, resolved.size_gated))

    # An explicit override governs every table directly and reads no rule, including a
    # refused one; only the unflagged path narrows to tables a threshold resolved for.
    judged_manifest = _judged_entries(manifest, resolved) if override is None else (manifest or {})
    stale = tuple(
        evaluate(
            judged_manifest,
            float(default_max_age_days),
            threshold_for=resolved.threshold_for if override is None else None,
        ),
    )

    offline_exit = EXIT_OK
    exit_moving_not_run = any(entry.severity == "error" for entry in not_run)

    if has_errors or exit_moving_not_run:
        offline_exit = max(offline_exit, EXIT_GENERIC)

    if stale:
        offline_exit = max(offline_exit, EXIT_STALENESS)

    # A conformance error or staleness leaves nothing worth comparing; a refused table does
    # not, so reading `offline_exit` here would cost the whole connection its drift check.
    offline_blocks_online = has_errors or bool(stale)

    # Offline statistic assertions run independent of `has_errors`, so a conformance error
    # and an assertion error can co-occur under the exit-code MAX rule.
    assertion_issues = _evaluate_offline_statistic_assertions(
        conn_config,
        print_root,
        manifest or {},
        on_table=_assertions_progress_adapter(renderer, conn_config.name),
    )

    online_drift: tuple[Issue, ...] = ()
    online_assertion_issues: tuple[Issue, ...] = ()
    drift_present = False
    connection_failed = False
    config_refused = False
    partial_scan = False
    online_disposition: OnlineDisposition = "not_requested"

    # An offline assertion error does not suppress the online phase - the print is still
    # well-formed and fresh; only `offline_blocks_online` means there is nothing to compare.
    if online and offline_blocks_online:
        online_disposition = "refused"
    elif online:
        outcome = _run_online(conn_config, project_root, renderer)
        online_drift = outcome.drift
        online_assertion_issues = outcome.statistic + outcome.sql
        connection_failed = outcome.connection_failed
        config_refused = outcome.config_refused
        partial_scan = outcome.partial
        drift_present = bool(outcome.drift)
        # `diff_result` is set only once `compute_diff` itself returned - a later reconnect
        # failure (SQL assertions) does not retroactively un-run the comparison it already made.
        comparison_ran = outcome.diff_result is not None
        online_disposition = (
            "assertions_connection_failed"
            if connection_failed and comparison_ran
            else "connection_failed"
            if connection_failed
            else "config_refused"
            if config_refused
            else "ran"
        )

        # A table the offline resolver refused can also fail inside the engine; the engine's
        # cause comes from the extraction that ran, so it replaces the offline entry.
        online_subjects = {entry.subject for entry in outcome.not_run}
        not_run = tuple(e for e in not_run if e.subject not in online_subjects) + outcome.not_run

    # Causes go to stderr as well: a pipeline log watcher does not read the stdout envelope.
    for entry in not_run:
        subject = "" if entry.subject == conn_config.name else f"{entry.subject}: "
        deferred.append(f"{conn_config.name}: {subject}{entry.cause}")

    combined_assertions = assertion_issues + online_assertion_issues
    has_assertion_error = any(i.severity == "error" for i in combined_assertions)

    exit_code = offline_exit

    if connection_failed:
        exit_code = max(exit_code, EXIT_CONNECTION)

    # A refused config is structural, not a connection failure: no retry changes the answer.
    if config_refused:
        exit_code = max(exit_code, EXIT_GENERIC)

    # A partial scan sits between a clean run and one that reached nothing; the tables that
    # did extract still contribute their own drift/assertion codes below.
    if partial_scan:
        exit_code = max(exit_code, EXIT_PARTIAL)

    if drift_present:
        exit_code = max(exit_code, EXIT_DRIFT)

    if has_assertion_error:
        exit_code = max(exit_code, EXIT_ASSERTION)

    result = CheckResult(
        connection_name=conn_config.name,
        print_root=str(print_root),
        manifest_present=True,
        issues=issues,
        stale_entries=stale,
        default_max_age_days=float(default_max_age_days),
        exit_code=exit_code,
        drift_issues=online_drift,
        assertion_issues=combined_assertions,
        not_run=not_run,
        online_disposition=online_disposition,
    )

    if renderer is not None:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        renderer.connection_summary(_summary_view(result, manifest, elapsed_ms))

    return result


def _not_run_from(
    resolved: thresholds.OfflineThresholds,
    *,
    exit_moving: bool,
) -> tuple[NotRun, ...]:
    """Tables the rule cascade refused, as the report's not-run entries.

    `exit_moving` is false under an explicit --max-age: the override governs freshness
    directly, so the refusal is reported as a warning rather than failing the run.
    """

    severity: Literal["error", "warning"] = "error" if exit_moving else "warning"

    return tuple(
        NotRun(subject=fqn, cause=cause, severity=severity)
        for fqn, cause in resolved.refused.items()
    )


def _judged_entries(
    manifest: dict[str, Any] | None,
    resolved: thresholds.OfflineThresholds,
) -> dict[str, Any]:
    """The manifest the freshness gate reads: the entries whose threshold settled.

    A refused table has no threshold to be judged against, and substituting the connection's
    would report a verdict derived from a configuration just refused; it is dropped here and
    reported as not run instead.
    """

    if manifest is None:
        return {}

    if not resolved.refused:
        return manifest

    tables = {
        fqn: entry
        for fqn, entry in (manifest.get("tables") or {}).items()
        if fqn not in resolved.refused
    }

    return {**manifest, "tables": tables}


def _fault_issues(connection_name: str, faults: tuple[ParseFault, ...]) -> tuple[Issue, ...]:
    """Turn parse-time faults (a malformed entry, a duplicate query name) into Issues.

    Each fault carries its own code and spec_ref; only the connection-qualified path is new.
    """

    return tuple(
        Issue(
            path=f"assertions.{connection_name}.{fault.path}",
            code=fault.code,
            severity="error",
            detail=fault.detail,
            spec_ref=fault.spec_ref,
        )
        for fault in faults
    )


def _evaluate_offline_statistic_assertions(
    conn_config: ConnectionConfig,
    print_root: Path,
    manifest: dict[str, Any],
    *,
    on_table: Callable[[str, int, int], None] | None = None,
) -> tuple[Issue, ...]:
    """Evaluate statistic assertion predicates against committed statistics.yaml."""

    try:
        assertion_set = parse_block(conn_config.assertions_raw)
    except ParseError as exc:
        return (
            Issue(
                path=f"assertions.{conn_config.name}",
                code="assertion.malformed-block",
                severity="error",
                detail=str(exc),
                spec_ref="ASSERTIONS.md §1.2",
            ),
        )

    fault_issues = _fault_issues(conn_config.name, assertion_set.faults)

    if not assertion_set.tables:
        return fault_issues

    stats_by_fqn = _load_committed_statistics(print_root, manifest, on_table=on_table)
    stat_issues = evaluate_statistic_assertions(assertion_set, conn_config.name, stats_by_fqn)

    return tuple(sorted(fault_issues + tuple(stat_issues)))


def _load_committed_statistics(
    print_root: Path,
    manifest: dict[str, Any],
    *,
    on_table: Callable[[str, int, int], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Read every table's committed statistics.yaml into the statistic assertion input shape.

    `on_table` fires once per table, after its own read - the progress adapter times the delta
    since the previous call, so ticking first would bill each table's cost to the next tick.
    """

    out: dict[str, dict[str, Any]] = {}
    tables = walkable_tables(manifest)
    total = len(tables)

    for i, (fqn, entry) in enumerate(tables.items(), start=1):
        try:
            entry_path = entry.get("path") or fqn.replace(".", "/")
            artifacts = declared_artifacts(entry)

            if "statistics" not in artifacts:
                continue

            stats_path = print_root / entry_path / artifacts["statistics"]

            if not stats_path.is_file():
                continue

            try:
                data = yaml.safe_load(stats_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                continue

            if isinstance(data, dict):
                out[fqn] = data
        finally:
            if on_table is not None:
                on_table(fqn, i, total)

    return out


@dataclass(frozen=True)
class _OnlineOutcome:
    """What the online half of one connection's check produced.

    `drift`/`statistic`/`sql` cover only tables actually compared; a run that compared nothing
    (unreachable database, refused config) and a partial scan's failed tables report through
    `not_run`. The booleans carry only the exit code: refused is 1, unreachable 4, partial 5.
    """

    drift: tuple[Issue, ...] = ()
    statistic: tuple[Issue, ...] = ()
    sql: tuple[Issue, ...] = ()
    connection_failed: bool = False
    config_refused: bool = False
    partial: bool = False
    not_run: tuple[NotRun, ...] = ()
    diff_result: DiffResult | None = None


def _run_online(
    conn_config: ConnectionConfig,
    project_root: Path,
    renderer: ProgressRenderer | None,
) -> _OnlineOutcome:
    """Run drift detection, live statistic assertions and SQL assertions against the live DB."""

    try:
        setup = build_engine(conn_config, project_root)
    except ConnectionSetupError as exc:
        return _OnlineOutcome(
            not_run=_not_run(conn_config.name, str(exc)),
            connection_failed=True,
        )
    except ConfigError as exc:
        return _OnlineOutcome(not_run=_not_run(conn_config.name, str(exc)), config_refused=True)

    adapter = setup.adapter
    on_progress = renderer.on_event if renderer is not None else None
    diff_result: DiffResult = setup.engine.compute_diff(DiffRequest(on_progress=on_progress))

    # An unreachable target is reported by a returned result, not a raise, and produces no
    # change events - reading the change list alone would call a scan that never ran clean.
    if diff_result.exit_code == EXIT_CONNECTION:
        cause = diff_result.failed_tables[0] if diff_result.failed_tables else "connection error"

        return _OnlineOutcome(
            not_run=_not_run(conn_config.name, cause),
            connection_failed=True,
        )

    # A partial result still has tables worth comparing, so no early return. `failed_tables`
    # here is FQNs, not the connection branch's exception string - one not-run entry each.
    partial_not_run = (
        tuple(
            NotRun(
                subject=fqn,
                cause="extraction failed for this table during the online scan; "
                "run `dbprint generate` to see the underlying cause",
            )
            for fqn in diff_result.failed_tables
        )
        if diff_result.exit_code == EXIT_PARTIAL
        else ()
    )
    partial = bool(partial_not_run)

    drift_issues = _drift_issues_from(conn_config.name, diff_result)

    # The offline half already reported every ParseError/fault, so re-parsing here only
    # recovers the usable tables/queries; reporting again would double every parse-time issue.
    try:
        assertion_set = parse_block(conn_config.assertions_raw)
    except ParseError:
        assertion_set = AssertionSet()

    statistic_issues: tuple[Issue, ...] = ()

    if assertion_set.tables:
        stat_issues = evaluate_statistic_assertions(
            assertion_set,
            conn_config.name,
            diff_result.live_statistics,
        )
        statistic_issues = tuple(sorted(stat_issues))

    sql_issues: tuple[Issue, ...] = ()

    if assertion_set.queries:
        try:
            adapter.connect()
        except Exception as exc:  # noqa: BLE001 - run-all-then-report; the error becomes not-run
            return _OnlineOutcome(
                drift=drift_issues,
                statistic=statistic_issues,
                not_run=partial_not_run + _not_run(conn_config.name, str(exc)),
                connection_failed=True,
                partial=partial,
                diff_result=diff_result,
            )

        # Tags the operator's own SQL the same way the engine tags every statement it sends.
        conn_token = trace_context.connection.set(conn_config.name)
        phase_token = trace_context.phase.set("execute_query")

        try:
            sql_issues = tuple(evaluate_sql_assertions(assertion_set, conn_config.name, adapter))
        finally:
            trace_context.phase.reset(phase_token)
            trace_context.connection.reset(conn_token)

            try:
                adapter.close()
            except Exception:  # noqa: BLE001, S110 - close-time failure is uninteresting
                pass

    return _OnlineOutcome(
        drift=drift_issues,
        statistic=statistic_issues,
        sql=sql_issues,
        not_run=partial_not_run,
        partial=partial,
        diff_result=diff_result,
    )


def _drift_issues_from(connection_name: str, diff_result: DiffResult) -> tuple[Issue, ...]:
    """Materialise compute_diff's drift events into Issues, pathed for grouping by table."""

    changes = diff_result.diff.get("changes") or []
    issues: list[Issue] = []

    for change in changes:
        kind = change.get("kind") or "drift"
        table = change.get("table") or change.get("source_table") or ""
        column = change.get("column")
        path_parts = [f"drift.{connection_name}", table]

        if column:
            path_parts.append(column)

        path_parts.append(kind)
        code = "drift.statistic-changed" if kind in DATA_CHANGE_KINDS else "drift.schema-changed"
        issues.append(
            Issue(
                path=".".join(p for p in path_parts if p),
                code=code,
                severity="error",
                detail=_drift_detail(change),
                spec_ref="ASSERTIONS.md §5.2",
            ),
        )

    return tuple(sorted(issues))


def _drift_detail(change: dict[str, Any]) -> str:
    parts = [f"{key}={value!r}" for key, value in change.items() if key != "kind"]

    return f"{change.get('kind', 'drift')}: " + ", ".join(parts)


def _not_run(connection_name: str, cause: str) -> tuple[NotRun, ...]:
    """One connection-wide cause as a not-run entry; nothing was read, so no table to name."""

    return (NotRun(subject=connection_name, cause=cause),)


def _load_manifest(print_root: Path) -> dict[str, Any] | None:
    path = print_root / "manifest.yaml"

    if not path.is_file():
        return None

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None

    if manifest_shape_error(data) is not None:
        return None

    return data if isinstance(data, dict) else None


def _print_root(conn: ConnectionConfig) -> Path:
    return conn.output / conn.name


def _assertions_progress_adapter(
    renderer: ProgressRenderer | None,
    connection_name: str,
) -> Callable[[str, int, int], None] | None:
    """Adapt the assertions walk's bare (fqn, index, total) callback into a `ProgressEvent` -
    `conformance` imports neither `engine` nor `cli`, so the translation lives here.
    """

    if renderer is None:
        return None

    last = time.monotonic()

    def on_table(fqn: str, index: int, total: int) -> None:
        nonlocal last
        now = time.monotonic()
        elapsed_ms = int((now - last) * 1000)
        last = now
        renderer.on_event(
            ProgressEvent(
                connection=connection_name,
                phase="assertions",
                status="done",
                index=index,
                total=total,
                fqn=fqn,
                elapsed_ms=elapsed_ms,
            ),
        )

    return on_table


def _validation_progress_adapter(
    renderer: ProgressRenderer | None,
    connection_name: str,
) -> ValidationProgress | None:
    """Adapt `validate_print`'s per-pass `ValidationTick` into one whole-command `ProgressEvent` -
    the bar spans every pass, so global index/total recompute from the tick's pass identity.
    """

    if renderer is None:
        return None

    last = time.monotonic()

    def on_tick(tick: ValidationTick) -> None:
        nonlocal last
        now = time.monotonic()
        elapsed_ms = int((now - last) * 1000)
        last = now
        renderer.on_event(
            ProgressEvent(
                connection=connection_name,
                phase="validate",
                status="done",
                index=(tick.pass_index - 1) * tick.total + tick.index,
                total=tick.pass_total * tick.total,
                fqn=tick.fqn,
                pass_name=tick.pass_name,
                pass_index=tick.pass_index,
                pass_total=tick.pass_total,
                findings=tick.findings,
                severity=tick.severity,
                elapsed_ms=elapsed_ms,
            ),
        )

    return on_tick


def _summary_view(
    result: CheckResult,
    manifest: dict[str, Any] | None,
    elapsed_ms: int,
) -> ConnectionSummary:
    """Project one connection's check outcome into the renderer's summary shape. A failure is
    matched by issue path; `elapsed_ms` is the caller's wall clock, not `diff_result`'s.
    """

    total = len(walkable_tables(manifest)) if manifest else 0
    not_run_count = len(result.not_run)
    failed_tables = _tables_with_errors(result.issues, manifest)
    failed_count = len(failed_tables)

    return ConnectionSummary(
        connection_name=result.connection_name,
        summary=SummaryCounts(
            ok=max(total - not_run_count - failed_count, 0),
            skipped=not_run_count,
            failed=failed_count,
        ),
        elapsed_ms=elapsed_ms,
        error=f"exit {result.exit_code}" if result.exit_code != EXIT_OK else None,
        tables=(
            tuple(
                TableResult(fqn=entry.subject, status="skipped", error=entry.cause, elapsed_ms=0)
                for entry in result.not_run
            )
            + tuple(
                TableResult(fqn=fqn, status="failed", error="conformance error", elapsed_ms=0)
                for fqn in sorted(failed_tables)
            )
        ),
    )


def _tables_with_errors(
    issues: tuple[Issue, ...],
    manifest: dict[str, Any] | None,
) -> set[str]:
    """FQNs carrying at least one error-severity issue, by directory-prefix match on its path."""

    if manifest is None:
        return set()

    dir_to_fqn = {
        entry.get("path", ""): fqn
        for fqn, entry in (manifest.get("tables") or {}).items()
        if isinstance(entry, dict) and entry.get("path")
    }
    found: set[str] = set()

    for issue in issues:
        if issue.severity != "error":
            continue

        parts = issue.path.split("/")

        for n in range(len(parts) - 1, 0, -1):
            fqn = dir_to_fqn.get("/".join(parts[:n]))

            if fqn is not None:
                found.add(fqn)

                break

    return found
