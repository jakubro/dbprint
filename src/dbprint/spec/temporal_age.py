"""Day-count arithmetic per SPEC 2.2.4, shared by every producer and the conformance validator.

`day_count` and `elapsed_days` (published instants, to the nanosecond) are SPEC 2.2.4's whole
elapsed days; every other helper derives from them, so the two sides can never round differently.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Literal


FreshnessClassification = Literal["live", "stale", "dormant"]

LIVE_CEILING_DAYS = 7
STALE_CEILING_DAYS = 90

_DAY_NANOSECONDS = 86_400_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FINE_FRACTION_RE = re.compile(r"\.\d{6}(\d{1,3})\d*(?=Z|[+-]\d{2}(?::?\d{2})?$|$)")


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


def sub_microsecond(value: object) -> int:
    """The nanoseconds past the microsecond a string's seventh to ninth fraction digits spell."""

    if not isinstance(value, str) or (match := _FINE_FRACTION_RE.search(value)) is None:
        return 0

    return int(match[1].ljust(3, "0"))


def instant_nanoseconds(value: object) -> int | None:
    """`value` as nanoseconds since the epoch, digits past the sixth kept; None as `parse_instant`.

    `datetime.fromisoformat` drops those digits, which a nanosecond type publishes (SPEC 2.2.4).
    """

    if (instant := parse_instant(value)) is None:
        return None

    return (instant - _EPOCH) // timedelta(microseconds=1) * 1000 + sub_microsecond(value)


def elapsed_days(earlier: object, later: object) -> int | None:
    """`day_count` between two published instants, to the nanosecond; None if one is unparseable."""

    start = instant_nanoseconds(earlier)
    end = instant_nanoseconds(later)

    if start is None or end is None:
        return None

    return (end - start) // _DAY_NANOSECONDS


def max_age_days(range_max: object, profiled_at: str) -> int:
    """SPEC 2.2.4: `max(0, day_count(max(column), profiled_at))`.

    Reads 0 when either operand is not a parseable instant - date-less, absent, or out of range.
    """

    days = elapsed_days(range_max, profiled_at)

    return 0 if days is None else max(0, days)


def freshness_classification(days: int) -> FreshnessClassification:
    """SPEC 2.2.4 thresholds: `live` under 7 days, `stale` under 90, `dormant` otherwise."""

    if days < LIVE_CEILING_DAYS:
        return "live"
    elif days < STALE_CEILING_DAYS:
        return "stale"
    else:
        return "dormant"


def negative_max_age_refusal(subject: str, value: float) -> str:
    """Why a negative `max_age_days` is refused, wherever one is read; `subject` names its site."""

    return (
        f"{subject} is {value}, which no print can ever satisfy: every table re-extracts on every "
        f"run and check reports every one of them stale. Use 0 to ask for that deliberately, or a "
        f"positive number of days."
    )
