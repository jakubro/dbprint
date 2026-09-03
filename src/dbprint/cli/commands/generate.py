"""`dbprint generate` - runs Engine.generate() per resolved connection."""

from __future__ import annotations

import contextlib
from pathlib import Path

import rich_click as click
from rich.console import Console

from dbprint.config import ConfigError, ConnectionConfig
from dbprint.engine import (
    EXIT_CONNECTION,
    EXIT_GENERIC,
    EXIT_OK,
    EXIT_TOTAL_FAILURE,
    GenerateRequest,
    GenerateResult,
    ProgressCallback,
)
from dbprint.engine.result import DiffSummary, SummaryCounts
from ..engine_setup import ConnectionSetupError, build_engine
from ..options import project_option, refuse_if_remote, resolve_project
from ..rendering import (
    build_progress_renderer,
    install_log_handler,
    remove_log_handler,
    resolve_render_mode,
    supports_live,
)
from ..rendering.errors import (
    connection_error_text,
    emit_error,
    failure_group_texts,
    sketch_failure_texts,
)
from ..resolution import ConnectionResolutionError, resolve
from ..run_log import close_run_log, log_run_header, log_run_summary, open_run_log


@click.command(name="generate")
@click.argument("conn", required=False)
@project_option
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Re-profile every matched table, bypassing the freshness skip.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Compute everything; write nothing to disk.",
)
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
    help="Also drop tables matching PATTERN (unions config exclude); repeatable. "
    "e.g. `--exclude '*.audit_*'`",
)
@click.option(
    "--fail-fast",
    is_flag=True,
    default=False,
    help="Stop at the first table failure instead of profiling the rest. Use when a "
    "target is failing systemically, to avoid repeating one doomed query per table.",
)
@click.option(
    "--tui/--no-tui",
    default=None,
    help="Force TTY (Rich) or piped (plain-text) rendering.",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    default=False,
    help="Silence stderr progress (footer / tree / streaming / summary) - generate writes "
    "nothing to stdout.",
)
@click.pass_context
def generate_command(
    ctx: click.Context,
    conn: str | None,
    project: str | None,
    force: bool,
    dry_run: bool,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    fail_fast: bool,
    tui: bool | None,
    quiet: bool,
) -> None:
    """Profile the live database; write prints and a structured diff.

    Connects to each resolved connection, scans the tables matched by the
    include/exclude selectors, extracts DDL + column statistics +
    relationships, and writes one print per table plus a `prints/<conn>/diff.yaml`
    describing what changed. Per-table writes are atomic and a user-authored
    `description.md` or `statistics.annotations.yaml` is never touched. Auto connections run
    sequentially, each isolated so one failure does not block the rest. Writes one run log to
    `~/.dbprint/logs/<project-slug>/`, keeping the 3 most recent.

    Selector patterns are fnmatch globs over lowercased FQNs (`*` spans dots,
    `?` matches one character); `--include` intersects and `--exclude` unions, so both only
    ever narrow scope.

    **Arguments:**

    - `CONN`: connection to profile; resolved from `.dbprint.yaml` when omitted
      (the `auto: true` set, or the sole connection).

    **Exit codes:**

    - `0`: ok (also when every matched table was already current, so nothing was profiled)
    - `1`: generic
    - `3`: schema drift (the database's shape moved relative to the baseline -
      a table, column, relationship, index or comment). Statistics that moved
      are recorded in `diff.yaml` but do not set this code;
      `dbprint check --online` reports both
    - `4`: connection
    - `5`: partial (some tables failed, others succeeded or were skipped; or every
      table succeeded but the sketch pass that runs after them did not)
    - `7`: total failure (no table was profiled)

    **Examples:**

    - `dbprint generate`: all auto connections
    - `dbprint generate warehouse`: one connection
    - `dbprint generate --include 'public.*'`: narrow scope for this run
    - `dbprint generate --dry-run`: preview plan + diff, write nothing
    - `dbprint generate --fail-fast`: stop at the first table failure
    """

    refuse_if_remote(project, "generate")
    project_config = resolve_project(project)

    try:
        connections = resolve(project_config, conn)
    except ConnectionResolutionError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    # Progress goes to stderr, matching `check` and `diff` - stdout carries no payload of its
    # own, so a redirected `dbprint generate | tee log` never mixes progress into it.
    console = Console(stderr=True)
    mode = resolve_render_mode(tui, console)

    if not quiet and tui is True and not supports_live(console):
        click.echo(
            "warning: --tui requested but stderr does not support the live view; "
            "using plain output",
            err=True,
        )

    out = click.get_text_stream("stderr")
    renderer = (
        None if quiet else build_progress_renderer(live=mode == "tty", console=console, out=out)
    )
    exit_codes: list[int] = []
    # Every stderr cause is deferred until the live footer stops printing.
    deferred: list[str] = []

    run_log = open_run_log(project_config.project_root, "generate")

    try:
        log_run_header(project_config.project_root, [c.name for c in connections])

        # quiet still installs a handler (an explicit swallow) - see install_log_handler.
        log_handler = install_log_handler(renderer)

        try:
            with renderer if renderer is not None else contextlib.nullcontext():
                for conn_config in connections:
                    # try/finally makes the flush structural, so a branch added here inherits it.
                    try:
                        result = _run_one(
                            conn_config,
                            project_config.project_root,
                            force=force,
                            dry_run=dry_run,
                            cli_include=include_patterns,
                            cli_exclude=exclude_patterns,
                            fail_fast=fail_fast,
                            on_progress=renderer.on_event if renderer is not None else None,
                        )

                        if renderer is not None:
                            renderer.connection_summary(result)

                        if result.error is not None:
                            deferred.append(
                                connection_error_text(result.connection_name, result.error),
                            )
                        elif not result.tables and result.exit_code == EXIT_OK:
                            deferred.append(
                                f"{conn_config.name}: no tables matched selectors "
                                f"(include={_effective_include(conn_config, include_patterns)}, "
                                f"exclude={_effective_exclude(conn_config, exclude_patterns)})",
                            )

                        deferred.extend(
                            failure_group_texts(
                                result.tables,
                                debug=bool(ctx.obj and ctx.obj.get("debug")),
                            ),
                        )
                        deferred.extend(sketch_failure_texts(result.sketch_failures))

                        if result.exit_code == EXIT_TOTAL_FAILURE:
                            deferred.append(
                                f"{result.connection_name}: no tables were profiled; "
                                f"all {result.summary.failed} failed",
                            )

                        if result.not_attempted:
                            deferred.append(
                                f"{result.connection_name}: stopped at the first failure "
                                f"(--fail-fast); {result.not_attempted} matched table(s) not "
                                "attempted, and the previous manifest is unchanged",
                            )
                        exit_codes.append(result.exit_code)
                    finally:
                        if renderer is not None:
                            renderer.flush_warnings()

                if renderer is not None:
                    renderer.finish()
        finally:
            remove_log_handler(log_handler)

        for text in deferred:
            emit_error(text)

        exit_code = max(exit_codes) if exit_codes else EXIT_OK
        log_run_summary(exit_code)
        ctx.exit(exit_code)
    finally:
        close_run_log(run_log)


def _effective_include(conn_config: ConnectionConfig, cli_include: tuple[str, ...]) -> list[str]:
    return list(cli_include) if cli_include else list(conn_config.include)


def _effective_exclude(conn_config: ConnectionConfig, cli_exclude: tuple[str, ...]) -> list[str]:
    return list(conn_config.exclude) + [e for e in cli_exclude if e not in conn_config.exclude]


def _run_one(
    conn_config: ConnectionConfig,
    project_root: Path,
    *,
    force: bool,
    dry_run: bool,
    cli_include: tuple[str, ...] = (),
    cli_exclude: tuple[str, ...] = (),
    fail_fast: bool = False,
    on_progress: ProgressCallback | None = None,
) -> GenerateResult:
    """Construct adapter + Engine for one connection; return GenerateResult."""

    try:
        setup = build_engine(conn_config, project_root)
    except ConnectionSetupError as exc:
        return _connection_failure(conn_config.name, str(exc))
    except ConfigError as exc:
        return _config_failure(conn_config.name, str(exc))

    request = GenerateRequest(
        force=force,
        dry_run=dry_run,
        cli_include=cli_include,
        cli_exclude=cli_exclude,
        on_progress=on_progress,
        fail_fast=fail_fast,
    )

    return setup.engine.generate(request)


def _connection_failure(name: str, message: str) -> GenerateResult:
    return _failure(name, message, EXIT_CONNECTION)


def _config_failure(name: str, message: str) -> GenerateResult:
    """A refused config exits generic: no retry reaches a `redact` rule missing its salt."""

    return _failure(name, message, EXIT_GENERIC)


def _failure(name: str, message: str, exit_code: int) -> GenerateResult:
    return GenerateResult(
        connection_name=name,
        tables=(),
        summary=SummaryCounts(ok=0, skipped=0, failed=0),
        diff_summary=DiffSummary(
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
        ),
        elapsed_ms=0,
        exit_code=exit_code,
        error=message,
    )
