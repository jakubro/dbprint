"""Day-count arithmetic per SPEC 2.2.4, shared by every producer and the conformance validator.

`day_count` is SPEC 2.2.4's one definition of whole elapsed days between two instants, and
every other helper derives from it, so the two sides can never round differently.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Literal


FreshnessClassification = Literal["live", "stale", "dormant"]

LIVE_CEILING_DAYS = 7
STALE_CEILING_DAYS = 90


def day_count(earlier: datetime, later: datetime) -> int:
    """Whole elapsed days between two instants: floor(elapsed seconds / 86400)."""

    return math.floor((later - earlier).total_seconds() / 86400)


def parse_instant(value: object) -> datetime | None:
    """`value` as a UTC-aware datetime, or None when it carries no date to compute against.

    A `datetime` passes through; a bare int is a MySQL YEAR value, read as Jan 1 of that
    year. Anything `fromisoformat` rejects - TIME-only, `infinity`, BC, a year outside
    0001-9999 - carries no date, and SPEC 2.2.4 forbids a day count against it. A naive
    reading is treated as UTC per SPEC 2.2.4, at the cost of an at-most-one-day residual.
    """

    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return datetime(value, 1, 1, tzinfo=UTC)
        except ValueError:
            return None

    if not isinstance(value, str):
        return None

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None

    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def max_age_days(range_max: object, profiled_at: str) -> int:
    """SPEC 2.2.4: `max(0, day_count(max(column), profiled_at))`.

    Reads 0 when either operand is not a parseable instant - date-less, absent, or out of range.
    """

    earlier = parse_instant(range_max)
    later = parse_instant(profiled_at)

    if earlier is None or later is None:
        return 0

    return max(0, day_count(earlier, later))


def freshness_classification(days: int) -> FreshnessClassification:
    """SPEC 2.2.4 thresholds: `live` under 7 days, `stale` under 90, `dormant` otherwise."""

    if days < LIVE_CEILING_DAYS:
        return "live"
    elif days < STALE_CEILING_DAYS:
        return "stale"
    else:
        return "dormant"
