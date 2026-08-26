"""Piped (plain-text) rendering for `dbprint list`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TextIO


def render_not_run_data(connection_name: str, causes: Sequence[str], out: TextIO) -> None:
    """Render one connection that produced no summary, and why.

    One line per cause, whitespace collapsed, on stdout so a stdout-only consumer sees why.
    """

    out.writelines(f"{connection_name}\tnot_run\t{' '.join(cause.split())}\n" for cause in causes)


def render_data(connection_name: str, summary: dict[str, object], out: TextIO) -> None:
    """Render `dbprint list` output for one connection as plain lines."""

    out.write(f"{connection_name}\tadapter\t{summary.get('adapter', '')}\n")
    out.write(f"{connection_name}\tgenerated_at\t{summary.get('generated_at', '')}\n")
    out.write(f"{connection_name}\ttable_count\t{summary.get('table_count', 0)}\n")
    out.write(f"{connection_name}\tlive\t{summary.get('live', 0)}\n")
    out.write(f"{connection_name}\tstale\t{summary.get('stale', 0)}\n")
    out.write(f"{connection_name}\tdormant\t{summary.get('dormant', 0)}\n")
    out.write(f"{connection_name}\tdescribed\t{summary.get('described', 0)}\n")
