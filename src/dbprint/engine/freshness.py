"""max_age duration parsing + manifest freshness evaluation for `dbprint check`; pure."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class StaleEntry:
    """One manifest entry whose `profiled_at` exceeds the threshold applied to it.

    `max_age_days` carries that threshold, which is resolved per table.
    """

    fqn: str
    age_days: float
    max_age_days: float


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
        age = age_days(entry.get("profiled_at"), current) if isinstance(entry, dict) else None

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


def age_days(profiled_at: Any, now: datetime) -> float | None:
    """Days between `profiled_at` and `now`, None when the stamp is absent or unreadable."""

    prior = parse_profiled_at(profiled_at)

    if prior is None:
        return None

    return (now - prior).total_seconds() / 86400.0


def parse_profiled_at(profiled_at: Any) -> datetime | None:
    """A `profiled_at` stamp as an aware instant, an offset-less one read as UTC."""

    if isinstance(profiled_at, datetime):
        prior = profiled_at
    elif isinstance(profiled_at, str) and profiled_at:
        try:
            prior = datetime.fromisoformat(profiled_at)
        except ValueError:
            return None
    else:
        return None

    return prior if prior.tzinfo is not None else prior.replace(tzinfo=UTC)
