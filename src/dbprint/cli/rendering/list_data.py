"""Piped plain-text and json/yaml rendering for `dbprint list`."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, TextIO

import yaml


def render_not_run_piped(connection_name: str, causes: Sequence[str], out: TextIO) -> None:
    """Render one connection that produced no summary, and why.

    One line per cause, whitespace collapsed, on stdout so a stdout-only consumer sees why.
    """

    out.writelines(f"{connection_name}\tnot_run\t{' '.join(cause.split())}\n" for cause in causes)


def render_piped(connection_name: str, summary: dict[str, object], out: TextIO) -> None:
    """Render `dbprint list` output for one connection as plain lines."""

    out.write(f"{connection_name}\tadapter\t{summary.get('adapter', '')}\n")
    out.write(f"{connection_name}\tgenerated_at\t{summary.get('generated_at', '')}\n")
    out.write(f"{connection_name}\ttable_count\t{summary.get('table_count', 0)}\n")
    out.write(f"{connection_name}\tlive\t{summary.get('live', 0)}\n")
    out.write(f"{connection_name}\tstale\t{summary.get('stale', 0)}\n")
    out.write(f"{connection_name}\tdormant\t{summary.get('dormant', 0)}\n")
    out.write(f"{connection_name}\tdescribed\t{summary.get('described', 0)}\n")


def render_data(entries: list[dict[str, Any]], fmt: str, stream: TextIO) -> None:
    """Emit every connection's `list` outcome as `fmt` (json array or yaml multi-document);
    `ok=True` carries the summary, `ok=False` carries `causes` - `check`'s split, by connection.
    """

    if fmt == "yaml":
        yaml.safe_dump_all(entries, stream, sort_keys=False, default_flow_style=False)
    else:
        json.dump(entries, stream, indent=2, default=str, sort_keys=False)
        stream.write("\n")
