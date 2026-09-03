"""Column classification per SPEC v1, section 3.

`classify()` picks the first match per the SPEC 3.2 priority order. Pure: no I/O, no state.
"""

from __future__ import annotations

import re
from typing import Literal


Classification = Literal[
    "boolean",
    "json",
    "foreign_key_candidate",
    "categorical",
    "temporal",
    "numeric",
    "text",
    "unsupported",
]


CANDIDATE_KEY_THRESHOLD = 0.9999

# Six-decimal rounding can carry a nonzero ratio to 0.0 or 1.0; these bounds keep a genuine
# zero distinguishable from "too small to show" (SPEC 2.2.6).
_RATIO_FLOOR = 0.000001
_RATIO_CEILING = 0.999999


def _floored(rounded: float, numerator: int) -> float:
    """A nonzero numerator never rounds all the way down to 0.0."""

    return _RATIO_FLOOR if numerator > 0 and rounded == 0.0 else rounded


def compute_cardinality_ratio(cardinality: int, rows_scanned: int) -> float:
    """The published `cardinality_ratio` (SPEC 2.2.6), rounded to six places.

    Single definition shared by adapters and the engine, so the 0.9999 candidate-key
    threshold falls the same way on both. A nonzero cardinality never rounds down to 0.0.
    """

    if not rows_scanned:
        return 0.0

    return _floored(round(cardinality / rows_scanned, 6), cardinality)


def is_candidate_key(cardinality: int, ratio: float) -> bool:
    """Whether an already-rounded ratio clears the SPEC 4.2 candidate-key threshold.

    Independent of `classify()` - a column of any classification can clear it.
    """

    return cardinality > 0 and ratio >= CANDIDATE_KEY_THRESHOLD


CandidateKeyException = Literal["measured_duplicates", "estimated"]


def compute_candidate_key_exception(
    cardinality: int,
    cardinality_ratio: float,
    cardinality_method: str,
    rows_scanned: int,
    null_count: int,
) -> CandidateKeyException | None:
    """The SPEC 4.2 `candidate_key_exception` marker (only relevant once ratio clears 0.9999).

    None at ratio 1.0 regardless of method. Below it: `measured_duplicates` when an exact count
    is short of the non-null scanned set, `estimated` when an estimate's error may straddle it.
    """

    if cardinality_ratio >= 1.0:
        return None

    if cardinality_method == "exact":
        return "measured_duplicates" if cardinality < rows_scanned - null_count else None

    return "estimated"


def compute_null_rate(null_count: int, rows_scanned: int) -> float:
    """The published `null_rate` (SPEC 2.2.6), rounded to six places.

    Single definition shared by all three adapters. Neither bound is reachable by rounding: a
    nonzero `null_count` never rounds down to 0.0, and a nonzero non-null count never rounds
    up to 1.0, since `null_rate: 1.0` is a defined sentinel (SPEC 2.2.7, 3.3).
    """

    if not rows_scanned:
        return 0.0

    rounded = round(null_count / rows_scanned, 6)
    non_null = rows_scanned - null_count

    if non_null > 0 and rounded == 1.0:
        return _RATIO_CEILING

    return _floored(rounded, null_count)


# MySQL has no native BOOLEAN - `BOOLEAN`/`BOOL` is an alias for `TINYINT(1)`, and `base_type()`
# strips the width that distinguishes it, so this is checked against the raw `sql_type`.
_MYSQL_BOOLEAN_TYPE = "tinyint(1)"

_BOOLEAN_TYPES = ("boolean", "bool")
_JSON_TYPES = ("json", "jsonb", "variant", "object", "super")
_TEMPORAL_TYPES = (
    "date",
    "date32",
    "time",
    "timestamp",
    "timestamp with time zone",
    "timestamp without time zone",
    "time with time zone",
    "time without time zone",
    "timestamp_ntz",
    "timestamp_ltz",
    "timestamp_tz",
    "datetime",
    "datetime64",
    "year",
)
_NUMERIC_TYPES = (
    "smallint",
    "integer",
    "bigint",
    "decimal",
    "numeric",
    "real",
    "double precision",
    "double",
    "float",
    "money",
    "number",
    "int",
    "tinyint",
    "mediumint",
    "int8",
    "int16",
    "int32",
    "int64",
    "int128",
    "int256",
    "uint8",
    "uint16",
    "uint32",
    "uint64",
    "uint128",
    "uint256",
    "float32",
    "float64",
    "decimal32",
    "decimal64",
    "decimal128",
    "decimal256",
    "hugeint",
    "ubigint",
    "uinteger",
    "usmallint",
    "utinyint",
    "bignumeric",
    "dec",
    "fixed",
)
_CHARACTER_TYPES = (
    "varchar",
    "text",
    "char",
    "character varying",
    "character",
    "string",
    "uuid",
    "fixedstring",
)
_UNSUPPORTED_TYPES = (
    "bytea",
    "blob",
    "binary",
    "varbinary",
    "image",
    "record",
    "struct",
    "array",
    "map",
    "tuple",
    "nested",
    "aggregatefunction",
    "simpleaggregatefunction",
)

# Matches one non-nested parenthesized group wherever it falls, not only at the end - some
# types qualify after the group, and some carry non-digit content a digit-only pattern misses.
_PRECISION_RE = re.compile(r"\([^()]*\)")

# MySQL reports these inside `column_type` (`bigint unsigned`, `int unsigned zerofill`),
# with no separating paren for a base-name split to key on.
_MYSQL_NUMERIC_QUALIFIER_RE = re.compile(r"\b(unsigned|zerofill|signed)\b")

# ClickHouse names a type by wrapping it (`Nullable(Int32)`, `LowCardinality(String)`) rather
# than qualifying it - the wrapped name is the type, so unwrapping recurses to it directly.
# The wrapper name is captured (not just discarded) so `is_nullable_type` can share this same
# definition rather than testing nullability with a second, unanchored pattern.
_CLICKHOUSE_WRAPPER_RE = re.compile(r"^(nullable|lowcardinality)\((.+)\)$")


def base_type(sql_type: str) -> str:
    """Lowercase type name with wrappers, precision/length and MySQL's qualifiers stripped - the
    one normalization every adapter's pre-classification and `classify` share.
    """

    lowered = sql_type.lower()

    while True:
        match = _CLICKHOUSE_WRAPPER_RE.match(lowered)

        if match is None:
            break

        lowered = match.group(2)

    stripped = _PRECISION_RE.sub("", lowered)
    stripped = _MYSQL_NUMERIC_QUALIFIER_RE.sub("", stripped)

    return " ".join(stripped.split())


def is_nullable_type(sql_type: str) -> bool:
    """Whether `sql_type` carries a `Nullable(...)` wrapper at any nesting depth.

    ClickHouse's `LowCardinality(Nullable(String))` nests it under a second wrapper, which an
    anchored test never matches, so this shares `base_type`'s own unwrapping regex.
    """

    lowered = sql_type.lower()

    while True:
        match = _CLICKHOUSE_WRAPPER_RE.match(lowered)

        if match is None:
            return False

        if match.group(1) == "nullable":
            return True

        lowered = match.group(2)


def classify(
    sql_type: str,
    cardinality: int | None,
    has_declared_fk: bool,
    enumeration_threshold: int,
    *,
    catalog_only: bool = False,
) -> Classification:
    """Return the v1 classification for a column per SPEC 3.2 priority order.

    `has_declared_fk` covers a declared or naming-inferred source, both catalog-derived, so it
    participates under `catalog_only` too. `cardinality=None` means either the adapter declined
    to profile (SPEC 3.1) or nothing was queried (`catalog_only`, SPEC 2.2.15); the two differ
    only in the unmatched-type fallthrough - `unsupported` and `text` respectively (SPEC 3.3).
    """

    base = base_type(sql_type)

    if _matches(base, _UNSUPPORTED_TYPES) or _is_array_type(sql_type):
        return "unsupported"
    elif _matches(base, _BOOLEAN_TYPES) or _is_mysql_boolean_type(sql_type):
        return "boolean"
    elif _matches(base, _JSON_TYPES):
        return "json"
    elif has_declared_fk:
        return "foreign_key_candidate"
    elif cardinality is not None and cardinality <= enumeration_threshold:
        return "categorical"
    elif _matches(base, _TEMPORAL_TYPES):
        return "temporal"
    elif _matches(base, _NUMERIC_TYPES):
        return "numeric"
    elif _matches(base, _CHARACTER_TYPES) or cardinality is not None or catalog_only:
        return "text"
    else:
        return "unsupported"


def is_string_like_type(sql_type: str) -> bool:
    """Whether `sql_type` could hold a string value, by elimination against the other classes -
    the shared test the matrix and every adapter's Phase A use to decide whether `length` applies.
    """

    base = base_type(sql_type)

    return not (
        _matches(base, _UNSUPPORTED_TYPES)
        or _is_array_type(sql_type)
        or _matches(base, _BOOLEAN_TYPES)
        or _matches(base, _JSON_TYPES)
        or _matches(base, _TEMPORAL_TYPES)
        or _matches(base, _NUMERIC_TYPES)
    )


# The temporal shapes with no day to truncate to (SPEC 2.2.3): DATE and DATE32 are always
# their own day-truncation, TIME carries no date at all, YEAR carries neither.
_NO_DAY_TEMPORAL_TYPES = (
    "date",
    "date32",
    "time",
    "time with time zone",
    "time without time zone",
    "year",
)


def has_day_resolution(sql_type: str) -> bool:
    """Whether `sql_type` is a temporal type `quantized_count`'s day-truncation applies to - the
    shared test the matrix and every adapter's temporal fetch use to decide whether to compute.
    """

    base = base_type(sql_type)

    return _matches(base, _TEMPORAL_TYPES) and not _matches(base, _NO_DAY_TEMPORAL_TYPES)


# TIME (with/without time zone) carries no date at all; YEAR carries only a year number.
# Neither has a calendar day/week/month a bucketing truncation could place.
_NO_CALENDAR_TEMPORAL_TYPES = (
    "time",
    "time with time zone",
    "time without time zone",
    "year",
)


def has_calendar_component(sql_type: str) -> bool:
    """Whether `sql_type` carries a calendar date `timeline` bucketing can truncate to - the
    anchor rule (SPEC 2.2.16) uses it, so `probe_timeline` can assume a calendar type.
    """

    base = base_type(sql_type)

    return _matches(base, _TEMPORAL_TYPES) and not _matches(base, _NO_CALENDAR_TEMPORAL_TYPES)


def _matches(base: str, types: tuple[str, ...]) -> bool:
    return base in types


def _is_array_type(sql_type: str) -> bool:
    return sql_type.rstrip().endswith("[]")


def _is_mysql_boolean_type(sql_type: str) -> bool:
    return sql_type.strip().lower() == _MYSQL_BOOLEAN_TYPE
