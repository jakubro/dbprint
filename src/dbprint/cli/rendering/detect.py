"""TTY vs piped detection with --tui/--no-tui override."""

from __future__ import annotations

import sys
from typing import Literal


RenderMode = Literal["tty", "piped"]


def resolve_render_mode(force_tui: bool | None) -> RenderMode:
    """Return the effective render mode."""

    if force_tui is True:
        return "tty"
    elif force_tui is False:
        return "piped"
    else:
        return "tty" if sys.stdout.isatty() else "piped"
