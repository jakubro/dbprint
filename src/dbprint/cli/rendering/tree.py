"""Pure layout helpers for the `generate`/`diff`/`check` TTY scrollback tree. `cap` is the
line-width budget (`min(120, terminal width)`), an invariant no rendered line may exceed.
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

    indent = min(depth * _INDENT, max(cap, 0))

    return " " * indent + _truncate_tail(name, cap - indent)


def leaf_metrics(depth: int, name: str, *, cap: int, rows: str, elapsed: str) -> str:
    """Render an ok leaf: indented name plus a fixed-width rows + elapsed block flush at `cap`."""

    block = f"{rows:>{_ROWS_WIDTH}}{' ' * _GAP}{elapsed:>{_ELAPSED_WIDTH}}"

    return _leaf_with_right_block(depth, name, block, cap=cap)


def leaf_findings(depth: int, name: str, *, cap: int, findings: int) -> str:
    """Render a validated leaf: indented name plus a right-anchored findings count."""

    block = f"{findings_text(findings):>{_RIGHT_BLOCK_WIDTH}}"

    return _leaf_with_right_block(depth, name, block, cap=cap)


def leaf_note(depth: int, name: str, note: str, *, cap: int) -> str:
    """Render a skipped leaf: indented name plus a right-anchored note (`(skipped)`)."""

    block = f"{note:>{_RIGHT_BLOCK_WIDTH}}"

    return _leaf_with_right_block(depth, name, block, cap=cap)


def leaf_duration(depth: int, name: str, *, cap: int, elapsed: str) -> str:
    """Render a leaf whose phase measures no rows (sketch, assertions): indented name plus a
    right-anchored duration alone, never a borrowed `- rows` from the row/elapsed pair.
    """

    block = f"{elapsed:>{_RIGHT_BLOCK_WIDTH}}"

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
    """Render a warning one level past `depth`, head-kept so the warning's subject survives.
    `max(0, ...)`, never a `_MIN_NAME` floor - a floor that clamps upward can outrun `cap`.
    """

    indent = min((depth + 1) * _INDENT, max(cap, 0))

    return " " * indent + _truncate_head(text, max(0, cap - indent))


def banner_box(text: str, *, cap: int) -> str:
    """A `cap`-wide rounded box, `text` centred on its middle line; three lines joined by `\\n`.
    No blank line above or below - the box is its own separation; its glyphs are display text.
    """

    inner = max(cap - 2, 0)
    label = _truncate_head(text, inner) if len(text) > inner else text
    top = "╭" + "─" * inner + "╮"
    middle = "│" + label.center(inner) + "│"
    bottom = "╰" + "─" * inner + "╯"

    return f"{top}\n{middle}\n{bottom}"


def rows_text(n: int | None) -> str:
    """Thousands separators, or a dash when unknown."""

    return f"{n:,} rows" if n is not None else "- rows"


def findings_text(n: int) -> str:
    """`N findings`, or `no findings` for zero - never `- rows`, which is `generate`'s idiom."""

    return "no findings" if n == 0 else f"{n:,} finding{'s' if n != 1 else ''}"


def objects_text(n: int | None) -> str:
    """Thousands separators, or a dash when unknown."""

    return f"{n:,} objects" if n is not None else "- objects"


def duration_text(ms: int | None) -> str:
    """One duration format for every elapsed time a user sees: `X.Xs` under a minute, `Xm Ys`
    under an hour, `Hh MMm` at an hour or more. A dash when unknown.
    """

    if ms is None:
        return "-"

    if ms < 60_000:
        return f"{ms / 1000:.1f}s"

    total_seconds = ms // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes:02d}m"

    return f"{minutes}m {seconds:02d}s"


def _leaf_with_right_block(depth: int, name: str, block: str, *, cap: int) -> str:
    """Fit `name` plus `block` inside `cap`, or shed `block` when the two cannot both fit - `cap`
    is an invariant, never a target, and `indent` is clamped to it for the same reason.
    """

    indent = min(depth * _INDENT, max(cap, 0))
    name_field = cap - indent - len(block) - _GAP

    if name_field < _MIN_NAME:
        return " " * indent + _truncate_tail(name, max(0, cap - indent))

    shown = _truncate_tail(name, name_field)

    return " " * indent + f"{shown:<{name_field}}" + " " * _GAP + block


def _truncate_tail(text: str, width: int) -> str:
    """Clip to `width` keeping the tail (`...name`), assuming names vary at the end, not a
    shared prefix.
    """

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
