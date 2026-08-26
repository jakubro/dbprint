"""The SPEC v1 section 2.2.3 field matrix, by classification.

Read by both the engine and the conformance validator, so the two cannot disagree about
which fields a classification carries. Only the flat rows live here: the two conditional
footnotes - a dropped bound, a prose value list - are derived procedurally by each reader.
"""

from __future__ import annotations


REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "boolean": frozenset(
        {
            "sql_type",
            "nullable",
            "null_count",
            "null_rate",
            "cardinality",
            "cardinality_ratio",
            "cardinality_method",
            "classification",
            "values",
            "values_coverage",
        },
    ),
    "json": frozenset(
        {
            "sql_type",
            "nullable",
            "null_count",
            "null_rate",
            "cardinality",
            "cardinality_ratio",
            "cardinality_method",
            "classification",
        },
    ),
    "foreign_key_candidate": frozenset(
        {
            "sql_type",
            "nullable",
            "null_count",
            "null_rate",
            "cardinality",
            "cardinality_ratio",
            "cardinality_method",
            "classification",
            "values",
            "values_coverage",
            "distribution",
        },
    ),
    "categorical": frozenset(
        {
            "sql_type",
            "nullable",
            "null_count",
            "null_rate",
            "cardinality",
            "cardinality_ratio",
            "cardinality_method",
            "classification",
            "values",
            "values_coverage",
            "distribution",
        },
    ),
    "temporal": frozenset(
        {
            "sql_type",
            "nullable",
            "null_count",
            "null_rate",
            "cardinality",
            "cardinality_ratio",
            "cardinality_method",
            "classification",
            "range",
            "percentiles",
            "freshness",
            "distribution",
            "frequencies",
        },
    ),
    "numeric": frozenset(
        {
            "sql_type",
            "nullable",
            "null_count",
            "null_rate",
            "cardinality",
            "cardinality_ratio",
            "cardinality_method",
            "classification",
            "range",
            "percentiles",
            "distribution",
            "frequencies",
        },
    ),
    "text": frozenset(
        {
            "sql_type",
            "nullable",
            "null_count",
            "null_rate",
            "cardinality",
            "cardinality_ratio",
            "cardinality_method",
            "classification",
            "values",
            "values_coverage",
            "distribution",
        },
    ),
    "unsupported": frozenset({"sql_type", "nullable", "null_count", "null_rate", "classification"}),
}

FORBIDDEN_FIELDS: dict[str, frozenset[str]] = {
    "boolean": frozenset(
        {"distribution", "range", "percentiles", "freshness", "unrepresentable", "frequencies"},
    ),
    "json": frozenset(
        {
            "values",
            "values_coverage",
            "values_coverage_method",
            "distribution",
            "range",
            "percentiles",
            "freshness",
            "redacted",
            "unrepresentable",
            "frequencies",
            "sketch",
        },
    ),
    "foreign_key_candidate": frozenset(
        {"range", "percentiles", "freshness", "unrepresentable", "frequencies"},
    ),
    "categorical": frozenset(
        {"range", "percentiles", "freshness", "unrepresentable", "frequencies"},
    ),
    "temporal": frozenset({"values", "values_coverage", "values_coverage_method"}),
    "numeric": frozenset(
        {"values", "values_coverage", "values_coverage_method", "freshness", "unrepresentable"},
    ),
    "text": frozenset({"range", "percentiles", "freshness", "unrepresentable", "frequencies"}),
    "unsupported": frozenset(
        {
            "cardinality",
            "cardinality_ratio",
            "cardinality_method",
            "values",
            "values_coverage",
            "values_coverage_method",
            "distribution",
            "range",
            "percentiles",
            "freshness",
            "inferred",
            "redacted",
            "unrepresentable",
            "frequencies",
            "sketch",
        },
    ),
}
