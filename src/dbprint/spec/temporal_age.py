"""Day-count arithmetic per SPEC 2.2.4, shared by every producer and the conformance validator.

`day_count` is SPEC 2.2.4's one definition of whole elapsed days between two instants, and
every other helper derives from it, so the two sides can never round differently.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal


FreshnessClassification = Literal["live", "stale", "dormant"]

LIVE_CEILING_DAYS = 7
STALE_CEILING_DAYS = 90


def day_count(earlier: datetime, later: datetime) -> int:
    """Whole elapsed days between two instants, fractional seconds included, with no float."""

    return (later - earlier) // timedelta(days=1)


def parse_instant(value: object) -> datetime | None:
    """`value` as a UTC-aware datetime, or None when it carries no date to compute against.

    A date reads as midnight UTC, an int as a YEAR's Jan 1, and a naive reading as UTC (SPEC 2.2.4).
    """

    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)

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
