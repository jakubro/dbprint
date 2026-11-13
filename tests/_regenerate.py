"""What a text naming a missing print file or an unprofiled table owes its reader: no command."""

from __future__ import annotations

import re


_IMPERATIVE_RE = re.compile(r"(?i)\b(re-?run|run)\s+`?dbprint\b")


def assert_leaves_regenerating_to_the_maintainer(text: str) -> None:
    """Fail when `text` tells its reader to run dbprint, or leaves out who regenerates and why."""

    assert not _IMPERATIVE_RE.search(text), text
    assert "queries the database" in text, text
    assert "whoever maintains the print" in text, text
