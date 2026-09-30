"""The single-unit `Nd`/`Nh`/`Nm`/`Ns` duration grammar every duration setting reads.

`--max-age` and `statement_timeout` share it, so the two settings cannot drift apart.
"""

from __future__ import annotations

import re


_DURATION_RE = re.compile(r"^(\d+)([dhms])$", re.IGNORECASE)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


class DurationError(ValueError):
    """Raised when a duration string does not match the `Nd`/`Nh`/`Nm`/`Ns` grammar."""


def parse_duration(value: str) -> float:
    """Convert `Nd` / `Nh` / `Nm` / `Ns` into days (float).

    Raises DurationError on anything else, compound forms like `1d12h` included.
    """

    return parse_duration_seconds(value) / 86400


def parse_duration_seconds(value: str) -> int:
    """Convert `Nd` / `Nh` / `Nm` / `Ns` into whole seconds; raises DurationError otherwise."""

    match = _DURATION_RE.match(value.strip())

    if not match:
        raise DurationError(
            f"invalid duration {value!r}. Expected `Nd`, `Nh`, `Nm`, or `Ns` (e.g. `7d`, `12h`).",
        )

    return int(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]


def format_threshold(days: float) -> str:
    """Render a threshold in the single-unit form `--max-age`/`parse_duration` accepts back - a
    configured value owes the reader a typeable form, unlike `format_age`'s compound `Xd Yh`.
    """

    return format_duration_seconds(round(days * 86400))


def format_duration_seconds(seconds: int) -> str:
    """Render whole seconds in the largest single unit that divides them exactly."""

    for unit in ("d", "h", "m"):
        if seconds % _UNIT_SECONDS[unit] == 0:
            return f"{seconds // _UNIT_SECONDS[unit]}{unit}"

    return f"{seconds}s"
