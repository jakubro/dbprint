"""TTY vs piped detection with --tui/--no-tui override. Every command resolves it through
`Console.is_terminal` on the console carrying its own output, so no two disagree.
"""

from __future__ import annotations

from typing import Literal

from rich.console import Console


RenderMode = Literal["tty", "piped"]


def resolve_render_mode(force_tui: bool | None, console: Console) -> RenderMode:
    """Return the effective render mode: forced by --tui/--no-tui, else detected on `console`."""

    if force_tui is True:
        return "tty"
    elif force_tui is False:
        return "piped"
    else:
        return "tty" if console.is_terminal else "piped"
