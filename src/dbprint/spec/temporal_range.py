"""Year-range classification for rendered temporal literals per SPEC 2.2.4.

A database can represent years outside proleptic-Gregorian `0001`-`9999` (Snowflake to
294276, Postgres `infinity`/BC). Classifying the rendered text needs no driver conversion
that could fail, and shared code makes all three adapters mark `unrepresentable` identically
(SPEC 2.2.4).
"""

from __future__ import annotations

import re


_LEADING_YEAR_RE = re.compile(r"^-?(\d+)-\d{2}-\d{2}")


def is_representable(rendered: str) -> bool:
    """True when `rendered` names a proleptic-Gregorian year in `0001`-`9999`.

    `rendered` is an adapter-produced SQL literal; a trailing `BC` marker or any shape other
    than `YYYY-MM-DD...` (a sentinel such as `infinity`) has no in-range year to read.
    """

    match = _LEADING_YEAR_RE.match(rendered)

    if match is None:
        return False

    if rendered.rstrip().endswith("BC"):
        return False

    year = int(match.group(1))

    return 1 <= year <= 9999
