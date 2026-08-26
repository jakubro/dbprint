"""Shared CLI error output: human-readable causes to stderr, structured output to stdout.

The single seam for "every non-zero exit prints a cause", in place of ad-hoc `click.echo`.
"""

from __future__ import annotations

from collections.abc import Iterable

import rich_click as click

from dbprint.engine import SketchFailure, TableResult


def emit_error(message: str) -> None:
    """Write a one-line human-readable error cause to stderr."""

    click.echo(message, err=True)


def connection_error_text(connection_name: str, cause: str) -> str:
    """A per-connection failure cause, naming the connection."""

    return f"{connection_name}: {cause}"


def failure_group_texts(tables: Iterable[TableResult], *, debug: bool = False) -> list[str]:
    """One block per distinct failure cause - a count, one example, `debug` traceback."""

    groups: dict[tuple[str, str | None], list[TableResult]] = {}

    for table in tables:
        if table.status == "failed":
            key = (table.error or "unknown error", table.error_operation)
            groups.setdefault(key, []).append(table)

    return [
        _failure_block(cause, operation, failed, debug=debug)
        for (cause, operation), failed in groups.items()
    ]


def sketch_failure_texts(failures: Iterable[SketchFailure]) -> list[str]:
    """One line per join-key column a sketch query failed on - never silent, never a table."""

    return [f"{f.table}.{f.column}: sketch query failed: {f.error}" for f in failures]


def _failure_block(
    cause: str,
    operation: str | None,
    failed: list[TableResult],
    *,
    debug: bool,
) -> str:
    first = failed[0]
    noun = "table" if len(failed) == 1 else "tables"
    lines = [f"{len(failed)} {noun} failed: {cause}"]

    if operation is not None:
        lines.append(f"  operation: {operation}")

    lines.append(f"  first: {first.fqn}")

    if first.error_detail is not None:
        lines.append(first.error_detail)

    if debug and first.error_traceback is not None:
        lines.append(first.error_traceback.rstrip())

    return "\n".join(lines)
