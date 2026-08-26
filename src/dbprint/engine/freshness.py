"""max_age duration parsing + manifest freshness evaluation for `dbprint check`; pure."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


_DURATION_RE = re.compile(r"^(\d+)([dhms])$", re.IGNORECASE)
_UNIT_TO_DAYS = {
    "d": 1.0,
    "h": 1.0 / 24.0,
    "m": 1.0 / (24.0 * 60.0),
    "s": 1.0 / (24.0 * 3600.0),
}


class DurationError(ValueError):
    """Raised when a duration string does not match the `Nd`/`Nh`/`Nm`/`Ns` grammar."""


@dataclass(frozen=True)
class StaleEntry:
    """One manifest entry whose `profiled_at` exceeds the threshold applied to it.

    `max_age_days` carries that threshold, which is resolved per table.
    """

    fqn: str
    age_days: float
    max_age_days: float


def parse_duration(value: str) -> float:
    """Convert `Nd` / `Nh` / `Nm` / `Ns` into days (float).

    Raises DurationError on anything else, compound forms like `1d12h` included.
    """

    match = _DURATION_RE.match(value.strip())

    if not match:
        raise DurationError(
            f"invalid duration {value!r}. Expected `Nd`, `Nh`, `Nm`, or `Ns` (e.g. `7d`, `12h`).",
        )

    n = int(match.group(1))
    unit = match.group(2).lower()

    return n * _UNIT_TO_DAYS[unit]


def evaluate(
    manifest: dict[str, Any],
    max_age_days: float,
    now: datetime | None = None,
    *,
    threshold_for: Callable[[str], float] | None = None,
) -> list[StaleEntry]:
    """Return entries from the manifest whose `profiled_at` is older than their threshold.

    `threshold_for` resolves the threshold per table, `max_age_days` applies when it is
    absent, and `now` defaults to `datetime.now(UTC)`. An entry that is not a mapping or has
    no parseable `profiled_at` is stale at infinite age rather than an error.
    """

    current = now or datetime.now(UTC)
    out: list[StaleEntry] = []

    for fqn, entry in (manifest.get("tables") or {}).items():
        threshold = threshold_for(fqn) if threshold_for is not None else max_age_days
        age = _age_days(entry.get("profiled_at"), current) if isinstance(entry, dict) else None

        if age is None:
            out.append(StaleEntry(fqn=fqn, age_days=float("inf"), max_age_days=threshold))
            continue

        if age > threshold:
            out.append(StaleEntry(fqn=fqn, age_days=age, max_age_days=threshold))

    out.sort(key=lambda s: (-s.age_days if s.age_days != float("inf") else float("-inf"), s.fqn))

    return out


def format_age(days: float) -> str:
    """Render age as `Xh` under a day, else `Xd Xh`; `unknown` for infinity."""

    if days == float("inf"):
        return "unknown"
    elif days < 1:
        hours = days * 24

        return f"{hours:.1f}h"
    else:
        return f"{int(days)}d {int((days - int(days)) * 24)}h"


def _age_days(profiled_at: str | None, now: datetime) -> float | None:
    if not profiled_at:
        return None

    try:
        prior = datetime.fromisoformat(profiled_at)
    except (ValueError, AttributeError):
        return None

    if prior.tzinfo is None:
        prior = prior.replace(tzinfo=UTC)

    return (now - prior).total_seconds() / 86400.0
