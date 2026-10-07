"""The labels a Markdown context reply prints, and the `## Terms` legend that defines them.

A renderer records the key of every label it prints; `legend` defines exactly those keys.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import get_args

from dbprint.spec.classification import CandidateKeyException
from dbprint.spec.distribution import Distribution
from dbprint.spec.epoch import EpochUnit
from dbprint.spec.looks_like import LooksLike
from dbprint.spec.redaction import Primitive
from dbprint.spec.sensitivity import Sensitivity
from dbprint.spec.temporal_age import FreshnessClassification


LEGEND_HEADING = "## Terms"

_PERCENTILE_KEY_RE = re.compile(r"^percentile:(\d+)$")


@dataclass(frozen=True)
class Term:
    """One printed label: its text, a one-line definition, and where json/yaml carries it."""

    label: str
    definition: str
    spelling: str | None = None
    tool_filter: str | None = None


def _members(
    family: str,
    values: Iterable[str],
    definition: str,
    field: str,
    *,
    tool_filter: str | None = None,
) -> dict[str, Term]:
    out = {}

    for value in values:
        word = value.replace("_", " ")
        out[f"{family}:{value}"] = Term(
            label=word,
            definition=definition.format(word=word),
            spelling=f"{field}: {value}",
            tool_filter=f"search_columns `{tool_filter}: {value}`" if tool_filter else None,
        )

    return out


_DISTRIBUTION_DEFINITIONS: dict[Distribution, str] = {
    "uniform": "the most frequent value holds at most twice the rows of the least frequent",
    "imbalanced": "the most frequent value holds more than twice the rows of the least frequent",
    "dominant_value": "one value holds at least 95% of the non-null scanned rows; named with its share",
    "long_tail": "the listed values cover under 30% of non-null scanned rows; many rarer values exist",
}

_PRINTED_CLASSIFICATIONS = (
    "boolean",
    "temporal",
    "numeric",
    "text",
    "json",
    "composite",
    "binary",
    "spatial",
    "vector",
)

TERMS: Mapping[str, Term] = {
    "scanned": Term(
        "Scanned",
        "rows the statistics were measured over, and their share of the table's rows",
        "scope.rows_scanned",
    ),
    "sampled": Term(
        "sampled",
        "a random sample was read; under it a row count - nulls, a value's count, a true/false "
        "split - MAY be multiplied by row_count / rows_scanned, and a distinct count, ratio, "
        "bound, percentile, sum or mean never is",
        "scope.sample",
    ),
    "filtered_by": Term(
        "filtered by",
        "a predicate chose the rows read, so nothing rescales to the table",
        "scope.filter",
    ),
    "over_the_rows_scanned": Term(
        "over the rows scanned",
        "the claim holds for the rows read; one unread row could falsify it for the table",
    ),
    "grain": Term("Grain", "the columns whose values identify a row", "grain"),
    "annotated": Term(
        "annotated",
        "named by a human in statistics.annotations.yaml, not measured",
        "statistics.annotations.yaml grain",
    ),
    "timeline": Term(
        "Timeline",
        "the anchor column's non-null values counted per bucket of the named unit",
        "timeline",
    ),
    "buckets": Term("buckets", "how many buckets hold at least one row", "timeline.buckets"),
    "bucket_starts": Term(
        "bucket starts",
        "the start of the first -> last bucket",
        "timeline.buckets[].start",
    ),
    "covers": Term(
        "covers",
        "on Timeline, the share of scanned rows the buckets hold; on an edge, the share of the "
        "target's distinct values the referencing column holds",
        "timeline.coverage / observed.target_coverage",
    ),
    "shown_combinations": Term(
        "Shown combinations",
        "each Rows share and the footer count only the null combinations listed, never the "
        "hidden ones",
        "null_patterns.patterns",
    ),
    "fk": Term(
        "FK",
        "every edge this column references that no human rejected, declared before inferred "
        "before measured, each with how it was found",
        "relationships.yaml refers_to",
    ),
    "fk_candidate": Term(
        "FK candidate",
        "classified as a foreign-key candidate, with no edge to name",
        "classification: foreign_key_candidate",
    ),
    "distinct": Term("distinct", "distinct non-null values in the scanned rows", "cardinality"),
    "values": Term(
        "values",
        "the listed values: every distinct value on a complete list, else the most frequent "
        "with their share of the non-null scanned rows",
        "values",
    ),
    "values_complete": Term(
        "values (complete)",
        "every distinct value, each with its share of the non-null scanned rows",
        "values, values_coverage: 1.0",
    ),
    "values_complete_scanned": Term(
        "values (complete over the rows scanned)",
        "every distinct value in the rows read, each with its share of the non-null scanned rows",
        "values, values_coverage: 1.0 under scope",
    ),
    "values_top": Term(
        "values (top N, covering X)",
        "the N most frequent values, each with its share of the non-null scanned rows; covering "
        "is the share those N hold together; the full list is in get_table_context with format: "
        "json or yaml, resolve_value checks whether one value occurs, and the query purpose "
        "shows the same 5",
        "values, values_coverage",
    ),
    "withheld": Term(
        "withheld",
        "a value hidden by a redaction rule; the count beside it is real",
    ),
    "true": Term("true", "rows holding true", "values"),
    "false": Term("false", "rows holding false", "values"),
    "range": Term(
        "range",
        "the true lowest -> highest value, with the days between them on a temporal column",
        "range.min, range.max, range.span_days",
    ),
    "percentile_band": Term(
        "P1-P99",
        "the 1st -> 99th percentile, where it differs from the range",
        "percentiles.p01, percentiles.p99",
    ),
    "span": Term(
        "span",
        "days between the lowest and highest value, the bounds themselves withheld",
        "range.span_days",
    ),
    "mean": Term("mean", "the arithmetic mean of the non-null scanned values", "mean"),
    "zeros": Term("zeros", "share of non-null scanned rows holding zero", "zero_count"),
    "negatives": Term(
        "negatives",
        "share of non-null scanned rows holding a negative value",
        "negative_count",
    ),
    "empty_strings": Term(
        "empty strings",
        "share of non-null scanned rows holding the empty string",
        "empty_count",
    ),
    "empty_arrays": Term(
        "empty arrays",
        "share of non-null scanned rows holding an empty array",
        "empty_count",
    ),
    "whole_numbers": Term(
        "whole numbers",
        "share of non-null scanned rows with no fractional part; not stated on an integer type",
        "quantized_count",
    ),
    "at_midnight": Term(
        "at midnight",
        "share of non-null scanned rows falling exactly on midnight",
        "quantized_count",
    ),
    "length": Term(
        "length",
        "shortest -> longest value in characters (bytes on a binary type), with the average",
        "length",
    ),
    "nulls": Term(
        "nulls",
        "share of scanned rows that are null; none = no nulls in the rows read; absent on a NOT "
        "NULL column with none",
        "null_rate, null_count",
    ),
    "nulls_column": Term(
        "Nulls",
        "share of scanned rows that are null; none = no nulls in the rows read; NOT NULL columns "
        "are not listed",
        "values.<column>.null_rate, nulls",
    ),
    "populated": Term(
        "populated",
        "the first -> last date the table's anchor column shows this column non-null",
        "populated",
    ),
    "unmeasured": Term(
        "unmeasured",
        "fields this run tried and failed to measure; their values are unknown, not absent",
        "unmeasured",
    ),
    "unrepresentable": Term(
        "unrepresentable",
        "fields whose value lies outside the years 0001-9999",
        "unrepresentable",
    ),
    "coverage_bounded": Term(
        "coverage bounded",
        "the coverage figure is a clamp: the list and the rows it covers were read apart",
        "values_coverage_method: bounded",
    ),
    "candidate_key": Term(
        "candidate key",
        "every scanned value is distinct - a measurement, never a declared constraint",
        "inferred.candidate_key",
    ),
    "cluster_partition_key": Term(
        "cluster/partition key",
        "the column is part of the table's declared clustering or partitioning key",
        "physical_layout_key",
    ),
    "looks_like": Term(
        "looks like",
        "the shape the sampled values match, with the sample it rests on; a guess",
        "inferred.looks_like",
        "search_columns `looks_like`",
    ),
    "near": Term(
        "near",
        "the best-matching shape below the verdict bar, with its share of the sample; no verdict",
        "inferred.looks_like_candidate",
    ),
    "detected": Term(
        "detected",
        "a sensitivity category the column suggests - a detection, never a verdict",
        "inferred.sensitivity",
        "search_columns `sensitivity`",
    ),
    "epoch": Term(
        "epoch",
        "an integer storing a Unix epoch instant in the named unit",
        "inferred.epoch_unit",
    ),
    "freshness": Term(
        "freshness",
        "how recent the newest value is against the configured age thresholds",
        "freshness.classification",
    ),
    "distribution": Term(
        "distribution",
        "how the non-null rows spread over the listed values",
        "distribution",
    ),
    "redacted": Term(
        "redacted",
        "a redaction rule withheld the values, naming its primitive; counts stay real",
        "redacted",
        "search_columns `redacted`",
    ),
    "parts": Term(
        "parts",
        "parts of the document or composite value profiled, of those found",
        "parts_found",
    ),
    "present": Term(
        "present",
        "share of the named population holding this member: rows, or instances of the parent part",
        "occurrences",
    ),
    "srid": Term("srid", "the spatial reference systems the geometries carry", "geometry.srids"),
    "extent_x": Term("extent x", "lowest -> highest x coordinate", "extent.min_x, extent.max_x"),
    "extent_y": Term("extent y", "lowest -> highest y coordinate", "extent.min_y, extent.max_y"),
    "dimension": Term("dimension", "every vector's dimension", "dimension"),
    "mixed_dimension": Term(
        "mixed dimension",
        "lowest -> highest vector dimension",
        "dimension",
    ),
    "unit_normalized": Term(
        "unit-normalized",
        "every vector has norm 1, so inner product ranks as cosine",
        "norm",
    ),
    "zero_vectors": Term("zero vectors", "scanned rows holding the zero vector", "zero_count"),
    "joins": Term(
        "Joins",
        "every edge relationships.yaml carries - declared, inferred or measured - except edges a "
        "human rejected; a join described only in the table's description is not in it",
        "joins.refers_to, joins.referenced_by",
    ),
    "via": Term("via", "the referencing column or columns", "refers_to[].column"),
    "on_delete": Term(
        "on delete",
        "the referential action the catalog declares for the edge",
        "on_delete",
    ),
    "fanout_avg": Term(
        "fanout avg",
        "average referencing rows per distinct key among rows that carry one",
        "observed.fanout_avg",
    ),
    "fanout_max": Term(
        "fanout max",
        "referencing rows on the most frequent key",
        "observed.fanout_max",
    ),
    "contained": Term(
        "contained",
        "share of the referencing column's distinct values found in the target; compared is how "
        "many hashes the two sketches could compare, the margin 1/sqrt(compared); exact when "
        "every distinct referencing value was compared",
        "observed.containment, observed.answerable_count",
    ),
    "not_measured": Term(
        "not measured",
        "one side was read in part, so nothing about the join was measured",
        "observed.scope_compatible: false",
    ),
    "incoherent": Term(
        "incoherent",
        "the referencing column has more distinct values than the target, so some cannot match",
        "observed.coherent: false",
    ),
    "detection:declared": Term(
        "declared",
        "the catalog declares it - a foreign key on an edge, a primary or unique key on Grain - "
        "so it is a constraint",
        "detection: declared",
    ),
    "detection:inferred": Term(
        "inferred",
        "guessed from column naming - a join candidate, never a constraint",
        "detection: inferred",
    ),
    "detection:measured": Term(
        "measured",
        "found by measuring values in the data - evidence about the rows read, never a constraint",
        "detection: measured",
    ),
    **{
        f"distribution:{shape}": Term(
            shape.replace("_", " "),
            _DISTRIBUTION_DEFINITIONS[shape],
            f"distribution: {shape}",
        )
        for shape in get_args(Distribution)
    },
    **_members(
        "freshness",
        get_args(FreshnessClassification),
        "the newest value reads {word} against the configured age thresholds",
        "freshness.classification",
    ),
    **_members(
        "looks_like",
        get_args(LooksLike),
        "the sampled values match the {word} shape",
        "inferred.looks_like",
        tool_filter="looks_like",
    ),
    **_members(
        "sensitivity",
        get_args(Sensitivity),
        "the column suggests {word} data",
        "inferred.sensitivity",
        tool_filter="sensitivity",
    ),
    **_members(
        "epoch_unit",
        get_args(EpochUnit),
        "the integer counts {word} since the Unix epoch",
        "inferred.epoch_unit",
    ),
    **_members(
        "candidate_key_exception",
        get_args(CandidateKeyException),
        "the key holds with an exception: {word}",
        "inferred.candidate_key_exception",
    ),
    **_members(
        "redaction",
        get_args(Primitive),
        "the redaction primitive {word} replaced or removed the values",
        "redacted",
        tool_filter="redacted",
    ),
    **_members(
        "classification",
        _PRINTED_CLASSIFICATIONS,
        "the column is classified {word}",
        "classification",
        tool_filter="classification",
    ),
}


def term(key: str) -> Term:
    """The term behind `key`; a percentile key (`percentile:50`) is generated for any number."""

    match = _PERCENTILE_KEY_RE.match(key)

    if match:
        n = int(match.group(1))

        return Term(
            label=f"P{n}",
            definition=f"the {_ordinal(n)} percentile of the non-null scanned values",
            spelling=f"percentiles.p{n:02d}",
        )

    return TERMS[key]


def label(key: str) -> str:
    """The exact text a renderer prints for `key`."""

    return term(key).label


def legend(keys: Iterable[str]) -> str:
    """`## Terms` and one line per key in TERMS order, percentiles last; "" for no keys."""

    wanted = set(keys)

    if not wanted:
        return ""

    order = list(TERMS)
    ordered = sorted(
        wanted,
        key=lambda k: (order.index(k), 0) if k in TERMS else (len(order), int(k.partition(":")[2])),
    )
    lines = [LEGEND_HEADING, ""]

    for key in ordered:
        t = term(key)
        where = [f"json/yaml `{t.spelling}`"] if t.spelling else []
        where += [t.tool_filter] if t.tool_filter else []
        suffix = f" ({'; '.join(where)})" if where else ""
        lines.append(f"- {t.label}: {t.definition}{suffix}")

    return "\n".join(lines)


def percentile_key(field: str) -> str | None:
    """The term key of a percentile field (`p50` -> `percentile:50`), else None."""

    match = re.match(r"^p(\d+)$", field)

    return f"percentile:{int(match.group(1))}" if match else None


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")

    return f"{n}{suffix}"
