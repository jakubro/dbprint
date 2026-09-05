"""TTY (Rich syntax) rendering for `dbprint context`."""

from __future__ import annotations

from rich.console import Console
from rich.syntax import Syntax


def render_human(text: str, console: Console) -> None:
    """Print `text` highlighted; needs `soft_wrap=True` on `console` and no `word_wrap` here,
    or wrapping/cropping at console width would corrupt the piped-byte parity this depends on.
    """

    if not text:
        return

    console.print(
        Syntax(text.rstrip("\n"), "markdown", theme="ansi_dark", background_color="default"),
    )
