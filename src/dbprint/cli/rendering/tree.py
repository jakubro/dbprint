"""Pure layout helpers for the `dbprint generate`/`dbprint diff` TTY scrollback tree.

Each completed table streams into scrollback as a leaf under a connection/schema (and, for
three-part FQNs, database) header path, with a fixed right-anchored rows + elapsed block so
those columns line up across every leaf. `cap` is the line-width budget of
`min(120, terminal width)`; a name exceeding its field is tail-truncated so the
distinguishing end survives, error text head-truncated so the exception kind does.
"""

from __future__ import annotations


_INDENT = 2
_ELLIPSIS = "..."
_ROWS_WIDTH = 22  # fits "9,999,999,999,999 rows"
_ELAPSED_WIDTH = 8  # fits "12345.6s"
_GAP = 3  # spaces between the leaf name and the numeric block
_MIN_NAME = 4  # never collapse the leaf-name field below this
_MAX_CAP = 120

_RIGHT_BLOCK_WIDTH = _ROWS_WIDTH + _GAP + _ELAPSED_WIDTH


def resolve_cap(terminal_width: int | None) -> int:
    """Return the line-width budget: min(120, terminal width); 120 when unknown."""

    if terminal_width is None:
        return _MAX_CAP

    return min(_MAX_CAP, terminal_width)


def header_path(connection: str, fqn: str) -> tuple[str, ...]:
    """Return the header chain for a table: connection then every FQN part but the leaf."""

    return (connection, *fqn.split(".")[:-1])


def leaf_name(fqn: str) -> str:
    """Return the table name - the last dotted FQN segment."""

    return fqn.split(".")[-1]


def divergent_headers(prev: tuple[str, ...], curr: tuple[str, ...]) -> list[tuple[int, str]]:
    """Return `(depth, name)` for header levels in `curr` not already printed by `prev`.

    An unchanged or shallower path yields the empty list, so a header is never reprinted -
    which relies on adapters listing tables in prefix-grouped order.
    """

    n = 0

    while n < len(prev) and n < len(curr) and prev[n] == curr[n]:
        n += 1

    return [(depth, curr[depth]) for depth in range(n, len(curr))]


def header_line(depth: int, name: str, *, cap: int) -> str:
    """Render one indented header (connection / database / schema) line."""

    indent = depth * _INDENT

    return " " * indent + _truncate_tail(name, cap - indent)


def leaf_metrics(depth: int, name: str, *, cap: int, rows: str, elapsed: str) -> str:
    """Render an ok leaf: indented name plus a fixed-width rows + elapsed block flush at `cap`."""

    block = f"{rows:>{_ROWS_WIDTH}}{' ' * _GAP}{elapsed:>{_ELAPSED_WIDTH}}"

    return _leaf_with_right_block(depth, name, block, cap=cap)


def leaf_note(depth: int, name: str, note: str, *, cap: int) -> str:
    """Render a skipped leaf: indented name plus a right-anchored note (`(skipped)`)."""

    block = f"{note:>{_RIGHT_BLOCK_WIDTH}}"

    return _leaf_with_right_block(depth, name, block, cap=cap)


def leaf_error(depth: int, name: str, error: str, *, cap: int) -> str:
    """Render a failed leaf: indented name then the error, head-kept, clipped to `cap`."""

    indent = depth * _INDENT
    prefix = " " * indent + name + " " * _GAP
    remaining = cap - len(prefix)

    if remaining < len(_ELLIPSIS):
        return _truncate_tail(" " * indent + name, cap)

    return prefix + _truncate_head(error, remaining)


def warning_line(depth: int, text: str, *, cap: int) -> str:
    """Render a warning one level past `depth`, head-kept so the warning's subject survives."""

    indent = (depth + 1) * _INDENT

    return " " * indent + _truncate_head(text, max(_MIN_NAME, cap - indent))


def rows_text(n: int | None) -> str:
    """Format a row count with thousands separators, or a dash when unknown."""

    return f"{n:,} rows" if n is not None else "- rows"


def secs_text(ms: int | None) -> str:
    """Format an elapsed millisecond span as seconds, or a dash when unknown."""

    return f"{ms / 1000:.1f}s" if ms is not None else "-"


def _leaf_with_right_block(depth: int, name: str, block: str, *, cap: int) -> str:
    indent = depth * _INDENT
    name_field = max(_MIN_NAME, cap - indent - len(block) - _GAP)
    shown = _truncate_tail(name, name_field)

    return " " * indent + f"{shown:<{name_field}}" + " " * _GAP + block


def _truncate_tail(text: str, width: int) -> str:
    """Clip to `width` keeping the tail (`...name`) so the distinguishing end survives."""

    if width <= 0:
        return ""

    if len(text) <= width:
        return text

    if width <= len(_ELLIPSIS):
        return text[-width:]

    return _ELLIPSIS + text[-(width - len(_ELLIPSIS)) :]


def _truncate_head(text: str, width: int) -> str:
    """Clip to `width` keeping the head (`name...`) so the message start survives."""

    if width <= 0:
        return ""

    if len(text) <= width:
        return text

    if width <= len(_ELLIPSIS):
        return text[:width]

    return text[: width - len(_ELLIPSIS)] + _ELLIPSIS
