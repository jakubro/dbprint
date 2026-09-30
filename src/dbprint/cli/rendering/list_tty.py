"""TTY (Rich panel) rendering for `dbprint list`."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


def render_human(connection_name: str, summary: dict[str, object], console: Console) -> None:
    """Render one connection's list summary as a Rich panel."""

    table = Table(show_header=False, box=None)

    for key in ("adapter", "generated_at", "table_count", "live", "stale", "dormant", "described"):
        table.add_row(key, str(summary.get(key, "")))

    failed = summary.get("failed_tables")

    if isinstance(failed, list) and failed:
        table.add_row(
            "failed_tables",
            f"{len(failed)} table(s) the last run could not profile: {', '.join(map(str, failed))}",
        )

    console.print(Panel.fit(table, title=connection_name))
