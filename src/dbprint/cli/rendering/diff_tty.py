"""TTY rendering for `dbprint diff` - Rich panel wrapping the text emitter.

Colors section markers (`+` green, `-` red, `~` yellow); content matches
`diff_data.render_human_text`, so piped consumers see the same data minus colors.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .diff_data import DiffRenderOptions, render_human_text


def render_human(diff_dict: dict[str, Any], options: DiffRenderOptions, console: Console) -> None:
    """Render one connection's diff as a colorized Rich Panel."""

    body = render_human_text(diff_dict, options)
    text = _colorize(body)
    conn_name = diff_dict.get("connection") or "(unknown)"
    panel = Panel(text, title=f"diff: {conn_name}", border_style="cyan")
    console.print(panel)


def _colorize(body: str) -> Text:
    """Apply section-marker colors (see module docstring) plus bold section headers."""

    text = Text()

    for line in body.splitlines(keepends=True):
        stripped = line.lstrip()

        if stripped.startswith(("+ ", "+ -")):
            text.append(line, style="green")
        elif stripped.startswith(("- ", "- -")):
            text.append(line, style="red")
        elif stripped.startswith(("~ ", "~ -")):
            text.append(line, style="yellow")
        elif stripped.endswith(":\n") and not stripped.startswith(" "):
            text.append(line, style="bold")
        else:
            text.append(line)

    return text
