"""Assertion-specific Issue code constants per ASSERTIONS.md 5.2.

The conformance suite owns the `Issue` dataclass; this module owns the `assertion.*` codes.
"""

from __future__ import annotations


# Configuration / discovery issues.

UNKNOWN_TABLE = "assertion.unknown-table"
UNKNOWN_COLUMN = "assertion.unknown-column"
UNKNOWN_STAT = "assertion.unknown-stat"
INAPPLICABLE_STAT = "assertion.inapplicable-stat"
REDACTED_STAT = "assertion.redacted-stat"
MALFORMED_PREDICATE = "assertion.malformed-predicate"
MALFORMED_BLOCK = "assertion.malformed-block"
DUPLICATE_QUERY_NAME = "assertion.duplicate-query-name"


# Statistic assertion predicate failures.

ROW_COUNT_MISMATCH = "assertion.row-count-mismatch"
NULL_COUNT_MISMATCH = "assertion.null-count-mismatch"
NULL_RATE_MISMATCH = "assertion.null-rate-mismatch"
CARDINALITY_MISMATCH = "assertion.cardinality-mismatch"
CARDINALITY_RATIO_MISMATCH = "assertion.cardinality-ratio-mismatch"
CLASSIFICATION_MISMATCH = "assertion.classification-mismatch"
DISTRIBUTION_MISMATCH = "assertion.distribution-mismatch"
ACCEPTED_VALUES_VIOLATED = "assertion.accepted-values-violated"
LOOKS_LIKE_MISMATCH = "assertion.looks-like-mismatch"
CANDIDATE_KEY_MISMATCH = "assertion.candidate-key-mismatch"
SQL_TYPE_MISMATCH = "assertion.sql-type-mismatch"
NULLABLE_MISMATCH = "assertion.nullable-mismatch"
RANGE_OUT_OF_BOUNDS = "assertion.range-out-of-bounds"
PERCENTILE_MISMATCH = "assertion.percentile-mismatch"
FRESHNESS_MISMATCH = "assertion.freshness-mismatch"
FRESHNESS_AGE_MISMATCH = "assertion.freshness-age-mismatch"


# SQL assertion query failures.

SQL_NON_ZERO = "assertion.sql-non-zero"
SQL_NON_EMPTY = "assertion.sql-non-empty"
SQL_EMPTY_RESULT = "assertion.sql-empty-result"
SQL_EXECUTION_ERROR = "assertion.sql-execution-error"
SQL_TYPE_COERCION_ERROR = "assertion.sql-type-mismatch"


# Stat name -> failure code lookup.

STAT_TO_FAILURE_CODE: dict[str, str] = {
    "row_count": ROW_COUNT_MISMATCH,
    "null_count": NULL_COUNT_MISMATCH,
    "null_rate": NULL_RATE_MISMATCH,
    "cardinality": CARDINALITY_MISMATCH,
    "cardinality_ratio": CARDINALITY_RATIO_MISMATCH,
    "classification": CLASSIFICATION_MISMATCH,
    "distribution": DISTRIBUTION_MISMATCH,
    "accepted_values": ACCEPTED_VALUES_VIOLATED,
    "looks_like": LOOKS_LIKE_MISMATCH,
    "candidate_key": CANDIDATE_KEY_MISMATCH,
    "sql_type": SQL_TYPE_MISMATCH,
    "nullable": NULLABLE_MISMATCH,
    "range.min": RANGE_OUT_OF_BOUNDS,
    "range.max": RANGE_OUT_OF_BOUNDS,
    "freshness.classification": FRESHNESS_MISMATCH,
    "freshness.max_age_days": FRESHNESS_AGE_MISMATCH,
}
