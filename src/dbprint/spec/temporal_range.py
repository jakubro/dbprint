"""Year-range classification for rendered temporal literals per SPEC 2.2.4.

A database can represent years outside proleptic-Gregorian `0001`-`9999` (Snowflake to
294276, Postgres `infinity`/BC). Classifying the rendered text needs no driver conversion
that could fail, and shared code makes all three adapters mark `unrepresentable` identically
(SPEC 2.2.4).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


_LEADING_YEAR_RE = re.compile(r"^([+-]?)(\d+)-\d{2}-\d{2}")

_INFINITY_RE = re.compile(r"^([+-]?)infinity$", re.IGNORECASE)


@dataclass(frozen=True)
class LeadingYear:
    """A rendered instant known only by its year: two values in one year cannot be ordered.

    `infinity` and `-infinity` carry an infinite year, so they sort past every other value.
    """

    year: float


def is_representable(rendered: str) -> bool:
    """True when `rendered` names a proleptic-Gregorian year in `0001`-`9999`.

    `rendered` is an adapter-produced SQL literal; a trailing `BC` marker or any shape other
    than `YYYY-MM-DD...` (a sentinel such as `infinity`) has no in-range year to read.
    """

    match = _LEADING_YEAR_RE.match(rendered)

    if match is None or match.group(1) == "-":
        return False

    if rendered.rstrip().endswith("BC"):
        return False

    year = int(match.group(2))

    return 1 <= year <= 9999


def leading_year(raw: object) -> LeadingYear | None:
    """The year a rendered temporal literal names, BC and signed years included, else None."""

    if not isinstance(raw, str):
        return None

    if (infinity := _INFINITY_RE.match(raw.strip())) is not None:
        return LeadingYear(-math.inf if infinity.group(1) == "-" else math.inf)

    if (match := _LEADING_YEAR_RE.match(raw)) is None:
        return None

    year = int(match.group(2))

    if match.group(1) == "-":
        return LeadingYear(-year)

    return LeadingYear(1 - year if raw.rstrip().endswith("BC") else year)
