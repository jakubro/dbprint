"""`dbprint list` - offline summary of committed prints per connection."""

from __future__ import annotations

import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import rich_click as click
from rich.console import Console

from dbprint.config.resolution import ConnectionResolutionError, resolve
from dbprint.engine import EXIT_GENERIC, EXIT_OK, thresholds
from dbprint.engine.baseline import (
    failed_tables,
    read_manifest,
    table_directory,
    unusable_manifest_message,
)
from dbprint.engine.freshness import classify
from dbprint.spec.artifacts import declared_artifacts
from ..options import project_option, resolve_project
from ..rendering import resolve_render_mode
from ..rendering.errors import emit_error
from ..rendering.list_data import render_data, render_not_run_piped, render_piped
from ..rendering.list_tty import render_human


@click.command(name="list")
@click.argument("connection", required=False)
@project_option
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
    help="Force TTY (Rich) or piped (plain-text) rendering. Human format only.",
)
@click.pass_context
def list_command(
    ctx: click.Context,
    connection: str | None,
    project: str | None,
    fmt: str,
    tui: bool | None,
) -> None:
    """Summarise committed prints offline (no database connection).

    Reads `prints/<connection>/manifest.yaml` and reports connection metadata, the
    table count, freshness buckets (live / stale / dormant) relative to
    each table's own `max_age_days`, and how many tables carry a user-authored
    `description.md`. Never connects to the database.

    **Arguments:**

    - `CONNECTION`: connection to summarize; resolved from `.dbprint.yaml` when
      omitted (the `auto: true` set, or the sole connection).

    **Exit codes:**

    - `0`: ok
    - `1`: a connection could not be summarised - its manifest is missing or
      unparseable, or its `rules` narrow one of its tables both by a predicate
      and by a fraction. Other connections are still summarised

    **Examples:**

    - `dbprint list`: all auto connections
    - `dbprint list warehouse`: one connection
    - `dbprint list --format json`: machine-readable summary
    """

    project_config = resolve_project(project)

    try:
        connections = resolve(project_config, connection)
    except ConnectionResolutionError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    fmt_lower = fmt.lower()
    console = Console()
    # --tui only chooses a rendering of the human format; json/yaml is always one combined
    # payload, ignoring --tui the same way `diff`'s own --format/--tui pair does.
    mode = resolve_render_mode(tui, console) if fmt_lower == "human" else "piped"
    overall_exit = EXIT_OK
    entries: list[dict[str, Any]] = []

    for conn_config in connections:
        read = read_manifest(conn_config.print_root)

        if read.manifest is None:
            _drop(
                conn_config.name,
                [unusable_manifest_message(read, conn_config.name)],
                mode,
                fmt_lower,
                entries,
            )
            overall_exit = max(overall_exit, EXIT_GENERIC)
            continue

        manifest = read.manifest
        resolved = thresholds.resolve(conn_config, manifest)

        # A table the cascade refuses has no threshold to bucket it by, so the counts would
        # describe fewer tables than `table_count` names; the connection is skipped whole.
        if resolved.refused:
            _drop(conn_config.name, list(resolved.refused.values()), mode, fmt_lower, entries)
            overall_exit = max(overall_exit, EXIT_GENERIC)
            continue

        if resolved.size_gated:
            click.echo(
                thresholds.size_gate_warning(conn_config.name, resolved.size_gated),
                err=True,
            )

        print_root = conn_config.print_root
        summary = _summarize_connection(manifest, resolved, print_root)

        if fmt_lower in {"json", "yaml"}:
            entries.append({"connection": conn_config.name, "ok": True, **summary})
        elif mode == "tty":
            render_human(conn_config.name, summary, console)
        else:
            render_piped(conn_config.name, summary, sys.stdout)

    if fmt_lower in {"json", "yaml"}:
        render_data(entries, fmt_lower, sys.stdout)

    ctx.exit(overall_exit)


def _summarize_connection(
    manifest: dict[str, Any],
    resolved: thresholds.OfflineThresholds,
    print_root: Path,
) -> dict[str, Any]:
    """Summarise one connection's manifest against its settled thresholds, bucketed by
    `engine.freshness.classify` so no surface disagrees about a verdict.
    """

    tables = manifest.get("tables", {})
    verdicts = classify(manifest, datetime.now(UTC), threshold_for=resolved.threshold_for)
    counted = Counter(verdict.verdict for verdict in verdicts.values())
    described = 0

    for fqn, entry in tables.items():
        # A declared-but-missing description does not count (SPEC 2.5): the manifest
        # promising a file is not the same fact as the file being there to read.
        artifacts = declared_artifacts(entry)

        if "description" in artifacts:
            table_dir = table_directory(print_root, fqn, entry)

            if (table_dir / artifacts["description"]).is_file():
                described += 1

    return {
        "adapter": manifest.get("adapter", ""),
        "generated_at": manifest.get("generated_at", ""),
        "table_count": len(tables),
        "live": counted["live"],
        "stale": counted["stale"],
        "dormant": counted["dormant"],
        "described": described,
        "failed_tables": list(failed_tables(manifest)),
    }


def _drop(
    name: str,
    causes: list[str],
    mode: str,
    fmt: str,
    entries: list[dict[str, Any]],
) -> None:
    """Report a connection this command could not summarise, on both channels.

    stderr always carries the cause; stdout carries it only in machine mode, where a consumer
    reading that stream alone cannot tell an absent connection from a filtered one.
    """

    for cause in causes:
        emit_error(f"{name}: {cause}")

    if fmt in {"json", "yaml"}:
        entries.append({"connection": name, "ok": False, "causes": list(causes)})
    elif mode != "tty":
        render_not_run_piped(name, causes, sys.stdout)
