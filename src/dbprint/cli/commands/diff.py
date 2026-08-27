"""`dbprint diff` - compare committed prints against the live database.

Read-only. Per resolved connection: verify a baseline manifest exists, build the adapter,
call `Engine.compute_diff()`, render the diff dict (human / json / yaml).
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import rich_click as click
from rich.console import Console

from dbprint.config import ConfigError, ConnectionConfig
from dbprint.engine import (
    EXIT_CONNECTION,
    EXIT_GENERIC,
    EXIT_OK,
    EXIT_PARTIAL,
    DiffRequest,
    ProgressCallback,
    SummaryCounts,
    TableResult,
)
from dbprint.engine.result import DiffResult
from ..engine_setup import ConnectionSetupError, build_engine
from ..options import project_option, refuse_if_remote, resolve_project
from ..rendering import (
    build_progress_renderer,
    install_log_handler,
    remove_log_handler,
    resolve_render_mode,
)
from ..rendering.diff_data import DiffRenderOptions, render_data, render_human_text
from ..rendering.diff_tty import render_human as render_human_tty
from ..rendering.errors import connection_error_text, emit_error
from ..rendering.progress import ConnectionSummary
from ..resolution import ConnectionResolutionError, resolve
from ..run_log import close_run_log, log_run_header, log_run_summary, open_run_log


@click.command(name="diff")
@click.argument("conn", required=False)
@project_option
@click.option(
    "--include",
    "include_patterns",
    multiple=True,
    help="Narrow scope to tables also matching PATTERN (intersects config include); "
    "repeatable. e.g. `--include 'public.*'`",
)
@click.option(
    "--exclude",
    "exclude_patterns",
    multiple=True,
    help="Also drop tables matching PATTERN (unions config exclude); repeatable.",
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
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write the diff to FILE instead of stdout.",
)
@click.option(
    "--threshold",
    "threshold",
    type=float,
    default=None,
    help="Minimum relative drift (0-1) before a statistic counts as changed; raises "
    "this run's noise floor. Human format only. e.g. 0.05 = ignore moves under 5%.",
)
@click.option(
    "--tui/--no-tui",
    default=None,
    help="Force TTY (Rich) or piped (plain-text) rendering; also drives stderr progress.",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    default=False,
    help="Silence stderr progress (footer / tree / streaming / summary); stdout payload unaffected.",
)
@click.pass_context
def diff_command(
    ctx: click.Context,
    conn: str | None,
    project: str | None,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    fmt: str,
    output_path: Path | None,
    threshold: float | None,
    tui: bool | None,
    quiet: bool,
) -> None:
    """Compare committed prints against the live database (read-only).

    Re-extracts the live schema + statistics and diffs them against the
    committed prints, emitting the same structured diff that `generate` writes -
    without touching disk on either side. Differences are reported, not
    failures: a successful comparison always exits 0. Progress goes to stderr
    so the diff payload on stdout stays clean for piping. Writes one run log to
    `~/.dbprint/logs/<project-slug>/`, keeping the 3 most recent, unaffected by
    `--quiet` (which silences stderr progress only).

    Selector patterns are fnmatch globs over lowercased FQNs (`*` spans dots);
    `--include` intersects and `--exclude` unions, so both only ever narrow scope.

    **Arguments:**

    - `CONN`: connection to compare; resolved from `.dbprint.yaml` when omitted
      (the `auto: true` set, or the sole connection).

    **Exit codes:**

    - `0`: ran (differences are not failures)
    - `1`: no baseline or invalid connection
    - `4`: connection
    - `5`: partial extraction

    **Examples:**

    - `dbprint diff`: compare all auto connections
    - `dbprint diff warehouse --format json`: machine-readable diff
    - `dbprint diff --threshold 0.05`: ignore statistic moves under 5%
    """

    refuse_if_remote(project, "diff")
    project_config = resolve_project(project)

    try:
        connections = resolve(project_config, conn)
    except ConnectionResolutionError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    fmt_lower = fmt.lower()
    results: list[_ConnectionDiff] = []
    overall_exit = EXIT_OK

    # Progress goes to stderr so stdout stays a clean diff payload; TTY-detect follows suit.
    err_console = Console(stderr=True)

    if not quiet and tui is True and not err_console.is_terminal:
        click.echo("warning: --tui requested but stderr is not a TTY; using plain output", err=True)

    renderer = (
        None
        if quiet
        else build_progress_renderer(
            live=_progress_live(tui, err_console),
            console=err_console,
            out=click.get_text_stream("stderr"),
        )
    )

    # Stderr causes are deferred until the live footer stops and prints its final frame -
    # nothing else may share the screen while it is up. Order is preserved.
    deferred: list[str] = []

    run_log = open_run_log(project_config.project_root, "diff")

    try:
        log_run_header(project_config.project_root, [c.name for c in connections])

        # quiet still installs a handler (an explicit swallow) - see install_log_handler.
        log_handler = install_log_handler(renderer)

        try:
            with renderer if renderer is not None else contextlib.nullcontext():
                for conn_config in connections:
                    # A warning queued while this connection ran must reach its own summary on
                    # every exit branch - connection_summary() flushes one, the finally the rest.
                    try:
                        if not _baseline_present(conn_config):
                            deferred.append(
                                f"No committed prints at prints/{conn_config.name}/. "
                                f"Run `dbprint generate {conn_config.name}` first.",
                            )
                            overall_exit = max(overall_exit, EXIT_GENERIC)

                            continue

                        result = _run_one(
                            conn_config,
                            project_config.project_root,
                            cli_include=include_patterns,
                            cli_exclude=exclude_patterns,
                            on_progress=renderer.on_event if renderer is not None else None,
                        )
                        overall_exit = max(overall_exit, result.exit_code)

                        if result.exit_code == EXIT_CONNECTION:
                            cause = (
                                result.failed_tables[0]
                                if result.failed_tables
                                else "connection error"
                            )
                            deferred.append(connection_error_text(result.connection_name, cause))

                            continue

                        # A refused config produced no comparison. Keyed on a cause, not the exit
                        # code: EXIT_GENERIC also covers a missing baseline, which emits one.
                        if result.exit_code == EXIT_GENERIC and result.failed_tables:
                            deferred.append(f"{result.connection_name}: {result.failed_tables[0]}")

                            continue

                        if result.exit_code == EXIT_PARTIAL and result.failed_tables:
                            deferred.append(
                                connection_error_text(
                                    result.connection_name,
                                    f"extraction failed for: {', '.join(result.failed_tables)}",
                                ),
                            )

                        # A selector matching no table is a clean, empty comparison, not a
                        # failure - reported at the exit code the run would otherwise earn.
                        if result.target_scanned_tables == 0 and result.exit_code == EXIT_OK:
                            deferred.append(
                                f"{conn_config.name}: no tables matched selectors "
                                f"(include={_effective_include(conn_config, include_patterns)}, "
                                f"exclude={_effective_exclude(conn_config, exclude_patterns)})",
                            )

                        if renderer is not None:
                            renderer.connection_summary(_summary_view(result))

                        results.append(_ConnectionDiff(config=conn_config, result=result))
                    finally:
                        if renderer is not None:
                            renderer.flush_warnings()

                if renderer is not None:
                    renderer.finish()
        finally:
            remove_log_handler(log_handler)

        for text in deferred:
            emit_error(text)

        if not results:
            log_run_summary(overall_exit)
            ctx.exit(overall_exit)

        if output_path is not None:
            with output_path.open("w") as fh:
                _emit(results, fmt_lower, threshold, fh, mode="piped")
        else:
            mode = resolve_render_mode(tui) if fmt_lower == "human" else "piped"
            stream = click.get_text_stream("stdout")
            _emit(results, fmt_lower, threshold, stream, mode=mode)

        log_run_summary(overall_exit)
        ctx.exit(overall_exit)
    finally:
        close_run_log(run_log)


@dataclass(frozen=True)
class _ConnectionDiff:
    """One connection's diff beside the config that decides how it renders.

    `DiffResult` names its connection but does not carry the config, and pairing later would
    mean matching by name against a list the skip branches have already thinned.
    """

    config: ConnectionConfig
    result: DiffResult


def _emit(
    results: list[_ConnectionDiff],
    fmt: str,
    threshold: float | None,
    stream: Any,
    *,
    mode: str,
) -> None:
    """Route results to the right renderer per format + TTY/piped mode."""

    if fmt in {"json", "yaml"}:
        render_data([r.result.diff for r in results], fmt, stream)

        return

    if mode == "tty":
        console = Console(file=stream, force_terminal=True)

        for pair in results:
            render_human_tty(pair.result.diff, _options_for(pair, threshold), console)

        return

    for pair in results:
        stream.write(render_human_text(pair.result.diff, _options_for(pair, threshold)))
        stream.write("\n")


def _progress_live(tui: bool | None, console: Console) -> bool:
    """Resolve live (footer) vs streaming progress, detecting on the stderr console."""

    if tui is True:
        return True
    elif tui is False:
        return False
    else:
        return console.is_terminal


def _summary_view(result: DiffResult) -> ConnectionSummary:
    """Project a DiffResult into the renderer's per-connection summary shape."""

    return ConnectionSummary(
        connection_name=result.connection_name,
        summary=SummaryCounts(
            ok=result.target_scanned_tables,
            skipped=0,
            failed=len(result.failed_tables),
        ),
        elapsed_ms=result.elapsed_ms,
        tables=tuple(
            TableResult(fqn=fqn, status="failed", error=None, elapsed_ms=0)
            for fqn in result.failed_tables
        ),
    )


def _options_for(pair: _ConnectionDiff, threshold: float | None) -> DiffRenderOptions:
    """Read the per-stat thresholds off the connection whose diff this is.

    Complete by construction: the parser seeds every key from the spec defaults before
    layering `defaults` and the connection's own block (SPEC 2.6.9).
    """

    return DiffRenderOptions(
        thresholds=dict(pair.config.diff.stat_change_threshold),
        threshold_override=threshold,
    )


def _baseline_present(conn_config: ConnectionConfig) -> bool:
    return (conn_config.output / conn_config.name / "manifest.yaml").is_file()


def _effective_include(conn_config: ConnectionConfig, cli_include: tuple[str, ...]) -> list[str]:
    return list(cli_include) if cli_include else list(conn_config.include)


def _effective_exclude(conn_config: ConnectionConfig, cli_exclude: tuple[str, ...]) -> list[str]:
    return list(conn_config.exclude) + [e for e in cli_exclude if e not in conn_config.exclude]


def _run_one(
    conn_config: ConnectionConfig,
    project_root: Path,
    *,
    cli_include: tuple[str, ...],
    cli_exclude: tuple[str, ...],
    on_progress: ProgressCallback | None = None,
) -> DiffResult:
    """Construct adapter + Engine for one connection; return DiffResult."""

    try:
        setup = build_engine(conn_config, project_root)
    except ConnectionSetupError as exc:
        return _connection_failure(conn_config.name, str(exc))
    except ConfigError as exc:
        return _config_failure(conn_config.name, str(exc))

    request = DiffRequest(cli_include=cli_include, cli_exclude=cli_exclude, on_progress=on_progress)

    return setup.engine.compute_diff(request)


def _connection_failure(name: str, message: str) -> DiffResult:
    return _failure(name, message, EXIT_CONNECTION)


def _config_failure(name: str, message: str) -> DiffResult:
    """A refused config exits generic - the target was never the problem."""

    return _failure(name, message, EXIT_GENERIC)


def _failure(name: str, message: str, exit_code: int) -> DiffResult:
    return DiffResult(
        connection_name=name,
        diff={
            "format_version": 1,
            "connection": name,
            "changes": [],
            "summary": {
                "tables_added": 0,
                "tables_removed": 0,
                "tables_modified": 0,
                "columns_added": 0,
                "columns_removed": 0,
                "columns_type_changed": 0,
                "columns_nullable_changed": 0,
                "columns_default_changed": 0,
                "statistics_drifted": 0,
                "relationships_changed": 0,
                "indexes_changed": 0,
                "comments_changed": 0,
                "unchanged_tables": 0,
                "unevaluated_tables": 0,
            },
        },
        target_scanned_tables=0,
        elapsed_ms=0,
        exit_code=exit_code,
        failed_tables=(message,),
    )
