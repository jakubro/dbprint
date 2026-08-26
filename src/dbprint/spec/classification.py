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


_BOOLEAN_TYPES = ("boolean",)
_JSON_TYPES = ("json", "jsonb", "variant", "object")
_TEMPORAL_TYPES = (
    "date",
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
)
_CHARACTER_TYPES = ("varchar", "text", "char", "character varying", "character", "string", "uuid")
_UNSUPPORTED_TYPES = ("bytea", "blob", "binary", "varbinary", "image", "record", "struct")

# Matches the group wherever it falls, not only at the end: `timestamp(3) with time zone`
# carries its qualifier after the group, which a first-`(` split would discard.
_PRECISION_RE = re.compile(r"\(\d+(?:,\s*\d+)?\)")

# MySQL reports these inside `column_type` (`bigint unsigned`, `int unsigned zerofill`),
# with no separating paren for a base-name split to key on.
_MYSQL_NUMERIC_QUALIFIER_RE = re.compile(r"\b(unsigned|zerofill|signed)\b")


def base_type(sql_type: str) -> str:
    """Lowercase type name with precision/length and MySQL's numeric qualifiers stripped.

    The one normalization every adapter's pre-classification and `classify` share, so
    `bigint unsigned` and `numeric(10,2)` reduce identically on every side.
    """

    stripped = _PRECISION_RE.sub("", sql_type.lower())
    stripped = _MYSQL_NUMERIC_QUALIFIER_RE.sub("", stripped)

    return " ".join(stripped.split())


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
    elif _matches(base, _BOOLEAN_TYPES):
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


def _matches(base: str, types: tuple[str, ...]) -> bool:
    return base in types


def _is_array_type(sql_type: str) -> bool:
    return sql_type.rstrip().endswith("[]")
