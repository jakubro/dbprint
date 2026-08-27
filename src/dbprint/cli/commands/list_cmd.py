"""`dbprint list` - offline summary of committed prints per connection.

Reads `prints/<conn>/manifest.yaml`: connection metadata, schema + table counts, freshness
buckets relative to `max_age_days`, and the count of tables carrying a `description.md`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import rich_click as click
import yaml
from rich.console import Console

from dbprint.engine import EXIT_GENERIC, EXIT_OK
from dbprint.engine.baseline import manifest_shape_error, walkable_tables
from .. import thresholds
from ..options import project_option, resolve_project
from ..rendering import resolve_render_mode
from ..rendering.errors import emit_error
from ..rendering.list_data import render_data, render_not_run_data
from ..rendering.list_tty import render_human
from ..resolution import ConnectionResolutionError, resolve


@click.command(name="list")
@click.argument("conn", required=False)
@project_option
@click.option(
    "--tui/--no-tui",
    default=None,
    help="Force TTY (Rich) or piped (plain-text) rendering.",
)
@click.pass_context
def list_command(
    ctx: click.Context,
    conn: str | None,
    project: str | None,
    tui: bool | None,
) -> None:
    """Summarise committed prints offline (no database connection).

    Reads `prints/<conn>/manifest.yaml` and reports connection metadata, table
    and schema counts, freshness buckets (live / stale / dormant) relative to
    each table's own `max_age_days`, and how many tables carry a user-authored
    `description.md`. Never connects to the database.

    **Arguments:**

    - `CONN`: connection to summarize; resolved from `.dbprint.yaml` when
      omitted (the `auto: true` set, or the sole connection).

    **Exit codes:**

    - `0`: ok
    - `1`: a connection could not be summarised - its manifest is missing or
      unparseable, or its `rules` narrow one of its tables both by a predicate
      and by a fraction. Other connections are still summarised

    **Examples:**

    - `dbprint list`: all auto connections
    - `dbprint list warehouse`: one connection
    """

    project_config = resolve_project(project)

    try:
        connections = resolve(project_config, conn)
    except ConnectionResolutionError as exc:
        click.echo(str(exc), err=True)
        ctx.exit(EXIT_GENERIC)

    mode = resolve_render_mode(tui)
    console = Console()
    overall_exit = EXIT_OK

    for conn_config in connections:
        manifest_path = conn_config.output / conn_config.name / "manifest.yaml"

        if not manifest_path.is_file():
            _drop(conn_config.name, [f"no manifest at {manifest_path}"], mode)
            overall_exit = max(overall_exit, EXIT_GENERIC)
            continue

        try:
            parsed = yaml.safe_load(manifest_path.read_text())
        except yaml.YAMLError as exc:
            _drop(conn_config.name, [f"could not parse {manifest_path}: {exc}"], mode)
            overall_exit = max(overall_exit, EXIT_GENERIC)
            continue

        # A manifest that parses but that no reader can walk is dropped like one that will not.
        unusable = manifest_shape_error(parsed) or _unwalkable_entry_reason(parsed)

        if unusable is not None:
            _drop(conn_config.name, [f"ignoring {manifest_path}: {unusable}"], mode)
            overall_exit = max(overall_exit, EXIT_GENERIC)
            continue

        manifest = parsed or {}
        resolved = thresholds.resolve(conn_config, manifest)

        # A table the cascade refuses has no threshold to bucket it by, so the counts would
        # describe fewer tables than `table_count` names; the connection is skipped whole.
        if resolved.refused:
            _drop(conn_config.name, list(resolved.refused.values()), mode)
            overall_exit = max(overall_exit, EXIT_GENERIC)
            continue

        if resolved.size_gated:
            click.echo(
                thresholds.size_gate_warning(conn_config.name, resolved.size_gated),
                err=True,
            )

        summary = _summarize_connection(manifest, resolved)

        if mode == "tty":
            render_human(conn_config.name, summary, console)
        else:
            render_data(conn_config.name, summary, click.get_text_stream("stdout"))

    ctx.exit(overall_exit)


def _summarize_connection(
    manifest: dict[str, Any],
    resolved: thresholds.OfflineThresholds,
) -> dict[str, Any]:
    """Summarise one connection's manifest against its settled thresholds."""

    tables = manifest.get("tables", {})
    now = datetime.now(UTC)
    live = stale = dormant = described = 0

    for fqn, entry in tables.items():
        # Each table is bucketed against its own threshold, so the counts agree with `check`.
        bucket = _freshness_bucket(
            entry.get("profiled_at"),
            now,
            resolved.threshold_for(fqn),
        )

        if bucket == "live":
            live += 1
        elif bucket == "stale":
            stale += 1
        elif bucket == "dormant":
            dormant += 1

        artifacts = entry.get("artifacts") or {}

        if isinstance(artifacts, dict) and "description" in artifacts:
            described += 1

    return {
        "adapter": manifest.get("adapter", ""),
        "generated_at": manifest.get("generated_at", ""),
        "table_count": len(tables),
        "live": live,
        "stale": stale,
        "dormant": dormant,
        "described": described,
    }


def _drop(name: str, causes: list[str], mode: str) -> None:
    """Report a connection this command could not summarise, on both channels.

    stderr always carries the cause; stdout carries it only in machine mode, where a consumer
    reading that stream alone cannot tell an absent connection from a filtered one.
    """

    for cause in causes:
        emit_error(f"{name}: {cause}")

    if mode != "tty":
        render_not_run_data(name, causes, click.get_text_stream("stdout"))


def _unwalkable_entry_reason(manifest: Any) -> str | None:
    """Why this manifest's tables cannot be summarised, or None when they can.

    An entry the shared rule cannot follow (not a mapping, or a non-string path) has no
    threshold and no artifacts, so it falls in no bucket.
    """

    declared = (manifest or {}).get("tables") or {}
    unwalkable = sorted(set(declared) - set(walkable_tables(manifest)))

    if not unwalkable:
        return None

    return f"no usable entry for {', '.join(unwalkable)}"


def _freshness_bucket(profiled_at: str | None, now: datetime, max_age_days: float) -> str:
    if not profiled_at:
        return "dormant"

    try:
        prior = datetime.fromisoformat(profiled_at)
    except (ValueError, AttributeError):
        return "dormant"

    age_days = (now - prior).total_seconds() / 86400.0

    if age_days < max_age_days:
        return "live"
    elif age_days < max_age_days * 13:  # ~90 days when max_age_days=7
        return "stale"
    else:
        return "dormant"
