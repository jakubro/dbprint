"""Per-classification "Notes" cell templates for `dbprint context`.

Pure: no I/O, no side effects. SPEC 2.2.2 mandates `classification` on every column dict.
A cell is facts joined by `; `, each `label: value` or a bare flag; a list joins by `, `.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from dbprint.spec.absence import Absence, column_value, read_column_field
from dbprint.spec.classification import is_binary_type, is_integer_type
from dbprint.spec.scope import ScanScope, list_is_complete, qualify, rows_scanned
from dbprint.spec.value_text import spell_number, spell_percent
from .context_terms import percentile_key
from .value_list import grouped_values, value_key
from .yaml_dumper import spell_literal


FACT_SEPARATOR = "; "
LIST_SEPARATOR = ", "
NOTES_TOP_VALUES_LIMIT = 5  # entries a truncated list shows in the Notes; a spelling group is one
UNIT_NORM_TOLERANCE = 0.001  # both norm bounds this close to 1 read as unit-normalized

_PERCENTILE_FIELD_RE = re.compile(r"^p(\d+)$")
_DETECTION_RE = re.compile(r"\((declared|inferred|measured)\)$")


@dataclass(frozen=True)
class Rendered:
    """Rendered text and the keys (`context_terms.TERMS`) of every label it prints."""

    text: str
    terms: frozenset[str] = frozenset()


def join_facts(facts: Iterable[Rendered], separator: str = "; ") -> Rendered:
    """Facts joined by `separator`, their terms combined; an empty fact is dropped."""

    kept = [fact for fact in facts if fact.text]

    return Rendered(
        separator.join(fact.text for fact in kept),
        frozenset().union(*(fact.terms for fact in kept)),
    )


def synthesize(
    column_stats: dict[str, Any],
    fk_targets: list[str] | None = None,
    *,
    hints_only: bool = False,
    statistics_params: dict[str, Any] | None = None,
    scope: ScanScope | None = None,
    row_count: int | None = None,
) -> Rendered:
    """Return the Notes cell for one column and the term keys its labels use.

    `hints_only` keeps the FK edges and suffixes; `row_count` is the fallback share population.
    """

    classification = column_value(column_stats, "classification") or "unsupported"
    params = statistics_params or {}

    facts = _fk_note(classification, fk_targets)

    if not hints_only:
        redaction = _redaction_primitive(column_stats)
        total = _non_null(column_stats, row_count, scope)
        rows = _non_null_rows(column_stats, row_count, scope)
        facts += _base_template(
            classification,
            column_stats,
            redaction,
            params,
            scope,
            total,
            rows,
        )

    facts = [
        *facts,
        *_candidate_key_suffix(column_stats, scope),
        *_physical_layout_key_suffix(column_stats),
        *_looks_like_suffix(column_stats, params),
        *_sensitivity_suffix(column_stats),
        *_epoch_unit_suffix(column_stats),
    ]

    if not hints_only:
        facts += [
            *_unmeasured_suffix(column_stats),
            *_unrepresentable_suffix(column_stats),
            *_null_suffix(column_stats, scope),
            *_coverage_method_suffix(column_stats),
            *_populated_suffix(column_stats),
        ]

    return join_facts(facts)


def enum_word(value: str) -> str:
    """An artifact enum value as the word a reader sees: `long_tail` reads `long tail`."""

    return value.replace("_", " ")


def percentile_label(field: str) -> str:
    """A percentile's label, `P50` for `p50` and `P1` for `p01`; any other name unchanged."""

    match = _PERCENTILE_FIELD_RE.match(field)

    return f"P{int(match.group(1))}" if match else field


def null_share(stats: dict[str, Any], scope: ScanScope | None) -> str | None:
    """`0.4%`, `none` or `none over the rows scanned`; None for a NOT NULL column with no nulls.

    Decided on the count, since SPEC 2.2.6 lifts a non-zero rate off 0.
    """

    null_count = column_value(stats, "null_count")
    null_rate = column_value(stats, "null_rate")

    if not _is_number(null_count) or not _is_number(null_rate):
        return None

    if null_count == 0:
        return qualify("none", scope) if column_value(stats, "nullable") is True else None

    return spell_percent(null_rate)


def _fact(text: str, *keys: str) -> Rendered:
    return Rendered(text, frozenset(keys))


def _qualified(fact: Rendered, scope: ScanScope | None) -> Rendered:
    """`qualify` on a fact, recording the clause's term when it applies."""

    if scope is None:
        return fact

    return Rendered(qualify(fact.text, scope), fact.terms | {"over_the_rows_scanned"})


def _base_template(
    classification: str,
    stats: dict[str, Any],
    redaction: str | None,
    params: dict[str, Any],
    scope: ScanScope | None,
    total: int | None,
    rows: int | None,
) -> list[Rendered]:
    """Dispatch on classification; the branches that render literals also read the marker.

    Read once here, not inside each helper: a branch that forgets to consult it renders a
    substitution as an observed value. Branches with no cell values are unaffected by
    redaction. Uniqueness is not a classification (SPEC 4.2) and arrives as a suffix.
    """

    if classification == "boolean":
        return _boolean_notes(stats, redaction)

    if classification == "categorical":
        return _categorical_notes(stats, redaction, params, scope, total)

    if classification == "foreign_key_candidate":
        entries = _value_entries(stats)
        redacted = [_redacted_label(redaction)] if redaction is not None else []

        if entries and list_is_complete(stats):
            values = [_complete_values(stats, entries, scope, total, redaction)]
        elif entries:
            values = [_truncated_values(stats, entries, total, redaction)]
        else:
            values = []

        return [
            *redacted,
            *values,
            *_distribution_suffix(stats, total, redaction),
            *_length_suffix(stats),
        ]

    if classification == "temporal":
        return _temporal_notes(stats, redaction, scope, total, rows)

    if classification == "numeric":
        return _numeric_notes(stats, redaction, total, rows)

    if classification == "text":
        return _text_notes(stats, redaction, params, scope, total, rows)

    if classification == "json":
        return [_fact("json", "classification:json"), *_types_suffix(stats), *_parts_suffix(stats)]

    if classification == "composite":
        return [
            _fact("composite", "classification:composite"),
            *_parts_suffix(stats),
            *_census(stats, rows, (("empty_count", "empty arrays", "empty_arrays"),)),
        ]

    if classification == "binary":
        return [_fact("binary", "classification:binary"), *_length_suffix(stats)]

    if classification == "spatial":
        return _spatial_notes(stats)

    if classification == "vector":
        return _vector_notes(stats)

    # unsupported and any future fallback
    return [_fact(column_value(stats, "sql_type") or "unsupported")]


def _fk_note(classification: str, fk_targets: list[str] | None) -> list[Rendered]:
    """Every edge the column references, surest first, on any classification (SPEC 2.3).

    A foreign-key candidate with no edge reads as the bare candidate flag.
    """

    if not fk_targets:
        return (
            [_fact("FK candidate", "fk_candidate")]
            if classification == "foreign_key_candidate"
            else []
        )

    detections = {f"detection:{m[1]}" for t in fk_targets if (m := _DETECTION_RE.search(t))}

    return [_fact("FK: " + LIST_SEPARATOR.join(fk_targets), "fk", *detections)]


def _boolean_notes(stats: dict[str, Any], redaction: str | None) -> list[Rendered]:
    """True/false split, or the two group sizes when the labels were withheld.

    Every redaction primitive breaks the pair lookup while leaving counts intact, and
    SPEC 2.2.4 orders by count rather than value, so which size was `true` is unrecoverable.
    """

    if redaction is not None:
        return [_redacted_label(redaction), *_withheld_values(_value_entries(stats))]

    if read_column_field(stats, "values").state is not Absence.PRESENT:
        return [_fact("boolean", "classification:boolean")]

    counts = _value_counts(stats)
    n_true = counts.get(True, counts.get("true", 0))
    n_false = counts.get(False, counts.get("false", 0))

    return [_fact(f"true: {n_true}", "true"), _fact(f"false: {n_false}", "false")]


def _categorical_notes(
    stats: dict[str, Any],
    redaction: str | None = None,
    params: dict[str, Any] | None = None,
    scope: ScanScope | None = None,
    total: int | None = None,
) -> list[Rendered]:
    entries = _value_entries(stats)
    complete = list_is_complete(stats)
    distribution = _distribution_suffix(stats, total, redaction)
    redacted = [_redacted_label(redaction)] if redaction is not None else []

    if complete and (entries or read_column_field(stats, "values").state is Absence.PRESENT):
        values = _complete_values(stats, entries, scope, total, redaction)

        return [*redacted, values, *distribution, *_length_suffix(stats)]

    if not entries:
        return [*redacted, *_distinct(stats), *distribution, *_length_suffix(stats)]

    values = _truncated_values(stats, entries, total, redaction)

    return [*redacted, values, *distribution, *_length_suffix(stats)]


def _temporal_notes(
    stats: dict[str, Any],
    redaction: str | None = None,
    scope: ScanScope | None = None,
    total: int | None = None,
    rows: int | None = None,
) -> list[Rendered]:
    percentiles = column_value(stats, "percentiles") or {}
    rng = column_value(stats, "range") or {}
    freshness = column_value(stats, "freshness") or {}
    low, high = rng.get("min"), rng.get("max")
    p01, p99 = percentiles.get("p01"), percentiles.get("p99")
    span = rng.get("span_days")
    freshness_class = freshness.get("classification")

    facts: list[Rendered] = []

    # Bounds carry the substitution; span_days/freshness are derived arithmetic, coarsened
    # rather than substituted (SPEC 2.2.9).
    if redaction is not None:
        facts.append(_redacted_label(redaction))

        if span is not None:
            facts.append(_fact(f"span: {span} days", "span"))
    else:
        if low is not None and high is not None:
            days = f" ({span} days)" if span is not None else ""
            facts.append(
                _fact(f"range: {spell_literal(low)} -> {spell_literal(high)}{days}", "range"),
            )

        if p01 is not None and p99 is not None and (p01, p99) != (low, high):
            band = f"{spell_literal(p01)} -> {spell_literal(p99)}"
            facts.append(_fact(f"P1-P99: {band}", "percentile_band"))

    facts += _sampled_values(stats, total, redaction)
    facts += _census(stats, rows, (("quantized_count", "at midnight", "at_midnight"),))
    facts += _distribution_suffix(stats, total, redaction)

    # No bucket means "not measured", so a column with no `freshness` block gets no verdict.
    if freshness_class:
        freshness_fact = _fact(
            f"freshness: {enum_word(freshness_class)}",
            "freshness",
            f"freshness:{freshness_class}",
        )
        facts.append(_qualified(freshness_fact, scope))

    return facts or [_fact("temporal", "classification:temporal")]


def _numeric_notes(
    stats: dict[str, Any],
    redaction: str | None = None,
    total: int | None = None,
    rows: int | None = None,
) -> list[Rendered]:
    """Range, median, mean and shape - under redaction only the count profile remains, so
    `distribution` stays and the bounds, mean and degenerate counts are absent (SPEC 2.2.9).
    """

    rng = column_value(stats, "range") or {}
    percentiles = column_value(stats, "percentiles") or {}
    mn, mx = rng.get("min"), rng.get("max")
    p50 = percentiles.get("p50")
    mean = column_value(stats, "mean")
    sql_type = column_value(stats, "sql_type")
    whole = isinstance(sql_type, str) and is_integer_type(sql_type)
    census = _census(
        stats,
        rows,
        (
            ("zero_count", "zeros", "zeros"),
            ("negative_count", "negatives", "negatives"),
            *([] if whole else [("quantized_count", "whole numbers", "whole_numbers")]),
        ),
    )
    distribution = _distribution_suffix(stats, total, redaction)

    if redaction is not None:
        facts = [_redacted_label(redaction)]

        if mean is not None:
            facts.append(_fact(f"mean: {_statistic(mean)}", "mean"))

        return [*facts, *_sampled_values(stats, total, redaction), *census, *distribution]

    facts = []

    if mn is not None and mx is not None:
        facts.append(_fact(f"range: {_statistic(mn)} -> {_statistic(mx)}", "range"))

    if p50 is not None:
        facts.append(_fact(f"P50: {_statistic(p50)}", "percentile:50"))

    if mean is not None:
        facts.append(_fact(f"mean: {_statistic(mean)}", "mean"))

    facts += [*_sampled_values(stats, total, redaction), *census, *distribution]

    return facts or [_fact("numeric", "classification:numeric")]


def _text_notes(
    stats: dict[str, Any],
    redaction: str | None = None,
    params: dict[str, Any] | None = None,
    scope: ScanScope | None = None,
    total: int | None = None,
    rows: int | None = None,
) -> list[Rendered]:
    """Top values and shape; `empty_count` needs no value list, so it reaches a prose column too."""

    entries = _value_entries(stats)
    distribution = _distribution_suffix(stats, total, redaction)
    census = _census(stats, rows, (("empty_count", "empty strings", "empty_strings"),))
    length = _length_suffix(stats)

    if not entries:
        return [_fact("text", "classification:text"), *distribution, *census, *length]

    if list_is_complete(stats):
        values = _complete_values(stats, entries, scope, total, redaction)
        redacted = [_redacted_label(redaction)] if redaction is not None else []

        return [*redacted, values, *distribution, *census, *length]

    values = _truncated_values(stats, entries, total, redaction)
    redacted = [_redacted_label(redaction)] if redaction is not None else []

    return [*redacted, values, *distribution, *census, *length]


def _distinct(stats: dict[str, Any]) -> list[Rendered]:
    cardinality = column_value(stats, "cardinality")

    return [] if cardinality is None else [_fact(f"distinct: {cardinality}", "distinct")]


def _sampled_values(
    stats: dict[str, Any],
    total: int | None,
    redaction: str | None,
) -> list[Rendered]:
    """A numeric or temporal column's frequency list, wherever it is not the whole domain."""

    entries = _value_entries(stats)

    if not entries or list_is_complete(stats):
        return []

    return [_truncated_values(stats, entries, total, redaction)]


def _truncated_values(
    stats: dict[str, Any],
    entries: list[tuple[Any, int]],
    total: int | None,
    redaction: str | None,
) -> Rendered:
    """The most frequent categories with their shares and the share they cover together.

    No figure counts or mentions an entry the list does not show.
    """

    shown = _grouped_entries(stats, entries)[:NOTES_TOP_VALUES_LIMIT]
    share_of = total or sum(count for _, count in entries) or 1
    items = [
        f"{'withheld' if redaction is not None else spell_literal(value)} "
        f"({spell_percent(count / share_of)}{_spelling_suffix(spellings)})"
        for value, count, spellings in shown
    ]
    covered = spell_percent(sum(count for _, count, _ in shown) / share_of)
    keys = ("values_top", "withheld") if redaction is not None else ("values_top",)

    return _fact(
        f"values (top {len(shown)}, covering {covered}): {LIST_SEPARATOR.join(items)}",
        *keys,
    )


def _distribution_suffix(
    stats: dict[str, Any],
    total: int | None,
    redaction: str | None,
) -> list[Rendered]:
    """The shape verdict (SPEC 2.2.5), schema-required on every classification that has it.

    Survives redaction; a dominant value is named with its share, as `withheld` when redacted.
    """

    distribution = column_value(stats, "distribution")

    if not distribution:
        return []

    text = f"distribution: {enum_word(distribution)}"
    grouped = _grouped_entries(stats, _value_entries(stats))

    if distribution == "dominant_value" and grouped:
        value, count, _ = grouped[0]
        shown = "withheld" if redaction is not None else spell_literal(value)
        share = f" ({spell_percent(count / total)})" if total else ""
        text += f" {shown}{share}"

    return [_fact(text, "distribution", f"distribution:{distribution}")]


def _complete_values(
    stats: dict[str, Any],
    entries: list[tuple[Any, int]],
    scope: ScanScope | None,
    total: int | None,
    redaction: str | None = None,
) -> Rendered:
    """A complete list, each category with its share of the non-null scanned rows."""

    key = "values_complete" if scope is None else "values_complete_scanned"
    label = "values (complete)" if scope is None else "values (complete over the rows scanned)"
    share_of = total or sum(count for _, count in entries) or 1
    items = []

    for value, count, spellings in _grouped_entries(stats, entries):
        shown = "withheld" if redaction is not None else spell_literal(value)
        items.append(f"{shown} ({spell_percent(count / share_of)}{_spelling_suffix(spellings)})")

    keys = (key, "withheld") if redaction is not None and items else (key,)

    return _fact(f"{label}: {LIST_SEPARATOR.join(items) or 'none'}", *keys)


def _census(
    stats: dict[str, Any],
    rows: int | None,
    members: tuple[tuple[str, str, str], ...],
) -> list[Rendered]:
    """Each census member present and non-zero, as its share of the non-null scanned rows.

    The bare count stands where no population is readable.
    """

    facts = []

    for field, label, key in members:
        count = column_value(stats, field)

        if isinstance(count, int) and not isinstance(count, bool) and count:
            share = spell_percent(count / rows) if rows else spell_number(count)
            facts.append(_fact(f"{label}: {share}", key))

    return facts


def _types_suffix(stats: dict[str, Any]) -> list[Rendered]:
    types = column_value(stats, "types")

    if not isinstance(types, dict) or not types:
        return []

    return [_fact(f"{name}: {spell_number(n)}") for name, n in types.items()]


def _parts_suffix(stats: dict[str, Any]) -> list[Rendered]:
    found = column_value(stats, "parts_found")
    parts = column_value(stats, "parts")

    if not isinstance(found, int):
        return []

    listed = len(parts) if isinstance(parts, dict) else 0

    if listed == found:
        return [_fact(f"parts: {spell_number(found)}", "parts")]

    return [_fact(f"parts: {spell_number(listed)} of {spell_number(found)} profiled", "parts")]


def _vector_notes(stats: dict[str, Any]) -> list[Rendered]:
    dimension = column_value(stats, "dimension")
    norm = column_value(stats, "norm")
    zero_count = column_value(stats, "zero_count")
    facts = [_fact("vector", "classification:vector")]

    if isinstance(dimension, dict):
        lo, hi = dimension["min"], dimension["max"]
        facts.append(
            _fact(f"dimension: {lo}", "dimension")
            if lo == hi
            else _fact(f"mixed dimension: {lo} -> {hi}", "mixed_dimension"),
        )

    if isinstance(norm, dict) and all(
        abs(norm[bound] - 1) <= UNIT_NORM_TOLERANCE for bound in ("min", "max")
    ):
        facts.append(_fact("unit-normalized", "unit_normalized"))

    if isinstance(zero_count, int) and zero_count:
        facts.append(_fact(f"zero vectors: {spell_number(zero_count)}", "zero_vectors"))

    return facts


def _spatial_notes(stats: dict[str, Any]) -> list[Rendered]:
    geometry = column_value(stats, "geometry")
    facts = [_fact("spatial", "classification:spatial")]

    if isinstance(geometry, dict):
        facts += [_fact(f"{k['kind']}: {spell_number(k['count'])}") for k in geometry["kinds"]]

        if srids := geometry.get("srids"):
            srid = LIST_SEPARATOR.join(str(s["srid"]) for s in srids)
            facts.append(_fact(f"srid: {srid}", "srid"))

    extent = column_value(stats, "extent")

    if isinstance(extent, dict):
        x = f"{_statistic(extent['min_x'])} -> {_statistic(extent['max_x'])}"
        y = f"{_statistic(extent['min_y'])} -> {_statistic(extent['max_y'])}"
        facts += [_fact(f"extent x: {x}", "extent_x"), _fact(f"extent y: {y}", "extent_y")]

    return facts


def _length_suffix(stats: dict[str, Any]) -> list[Rendered]:
    """The length span (SPEC 2.2.4) - bytes on a binary type - silent wherever none is carried."""

    length = column_value(stats, "length")

    if not isinstance(length, dict):
        return []

    mn, mx, avg = length.get("min"), length.get("max"), length.get("avg")

    if mn is None or mx is None or avg is None:
        return []

    sql_type = column_value(stats, "sql_type")
    unit = " bytes" if isinstance(sql_type, str) and is_binary_type(sql_type) else ""

    span = f"{_statistic(mn)} -> {_statistic(mx)}{unit} (avg {_statistic(avg)})"

    return [_fact(f"length: {span}", "length")]


def _redaction_primitive(stats: dict[str, Any]) -> str | None:
    """The `redacted` marker naming the primitive, or None when the values are real.

    The artifact is hand-editable, so a non-string marker counts as absent rather than
    reaching the cell as though it were a primitive name.
    """

    marker = column_value(stats, "redacted")

    return marker if isinstance(marker, str) and marker else None


def _redacted_label(primitive: str) -> Rendered:
    """How a withheld cell announces itself, naming the primitive the artifact declares."""

    return _fact(f"redacted: {primitive}", "redacted", f"redaction:{primitive}")


def _withheld_values(entries: list[tuple[Any, int]]) -> list[Rendered]:
    if not entries:
        return []

    listed = LIST_SEPARATOR.join(f"withheld ({count})" for _, count in entries)

    return [_fact(f"values: {listed}", "values", "withheld")]


def _candidate_key_suffix(stats: dict[str, Any], scope: ScanScope | None = None) -> list[Rendered]:
    """A suffix, not a branch: `inferred.candidate_key` (SPEC 4.2) rides every classification.

    Names the exception when the ratio falls short of 1.0, so the cell does not overclaim.
    """

    if not column_value(stats, "inferred.candidate_key"):
        return []

    exception = column_value(stats, "inferred.candidate_key_exception")

    if exception is not None:
        fact = _fact(
            f"candidate key ({enum_word(exception)})",
            "candidate_key",
            f"candidate_key_exception:{exception}",
        )

        return [_qualified(fact, scope)]

    return [_qualified(_fact("candidate key", "candidate_key"), scope)]


def _physical_layout_key_suffix(stats: dict[str, Any]) -> list[Rendered]:
    """A suffix, not a branch: the marker is orthogonal to every classification above."""

    marked = column_value(stats, "physical_layout_key")

    return [_fact("cluster/partition key", "cluster_partition_key")] if marked else []


def _populated_suffix(stats: dict[str, Any]) -> list[Rendered]:
    """A suffix: the window the data itself shows this column non-null, dated against the
    table's own anchor column (SPEC 2.2.4) - never a claim about when the schema changed.
    """

    populated = column_value(stats, "populated")

    if not isinstance(populated, dict):
        return []

    start, end = populated.get("from"), populated.get("to")

    if not (start and end):
        return []

    return [_fact(f"populated: {spell_literal(start)} -> {spell_literal(end)}", "populated")]


def _looks_like_suffix(
    stats: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> list[Rendered]:
    """A suffix: the detected shape (SPEC 4.1), present only where SPEC 4.1.5 samples for it -
    absent a verdict, `_looks_like_candidate_suffix` covers the near-miss instead.
    """

    pattern = column_value(stats, "inferred.looks_like")

    if not pattern:
        return _looks_like_candidate_suffix(stats)

    word = enum_word(pattern)
    keys = ("looks_like", f"looks_like:{pattern}")
    sampled = column_value(stats, "inferred.sampled")
    matched = column_value(stats, "inferred.matched")
    has_evidence = (
        isinstance(sampled, int)
        and isinstance(matched, int)
        and not isinstance(sampled, bool)
        and not isinstance(matched, bool)
    )
    sample_size = (params or {}).get("looks_like_sample_size")
    has_configured = isinstance(sample_size, int) and not isinstance(sample_size, bool)

    if has_evidence and has_configured:
        evidence = f" ({matched} of {sampled} sampled, {sample_size} configured)"
    elif has_evidence:
        evidence = f" ({matched} of {sampled} sampled)"
    elif has_configured:
        evidence = f" (drawn {sample_size})"
    else:
        evidence = ""

    return [_fact(f"looks like: {word}{evidence}", *keys)]


def _looks_like_candidate_suffix(stats: dict[str, Any]) -> list[Rendered]:
    """`looks_like`'s near-miss (SPEC 4.1.3): the best-scoring pattern below the verdict bar,
    worded "near" so it cannot read as the verdict, with the share named against the sample.
    """

    candidate = column_value(stats, "inferred.looks_like_candidate")
    share = column_value(stats, "inferred.looks_like_candidate_share")

    if not candidate or not isinstance(share, (int, float)) or isinstance(share, bool):
        return []

    text = f"near: {enum_word(candidate)} ({spell_percent(share)} of sampled values, no verdict)"

    return [_fact(text, "near", f"looks_like:{candidate}")]


def _sensitivity_suffix(stats: dict[str, Any]) -> list[Rendered]:
    """A suffix, a detection and never a verdict: SPEC 4.4 gates redaction, it does not rule."""

    category = column_value(stats, "inferred.sensitivity")

    if not category:
        return []

    return [_fact(f"detected: {enum_word(category)}", "detected", f"sensitivity:{category}")]


def _epoch_unit_suffix(stats: dict[str, Any]) -> list[Rendered]:
    """A suffix: an integer storing a Unix epoch instant rather than a plain quantity (SPEC 4.5)."""

    unit = column_value(stats, "inferred.epoch_unit")

    return [_fact(f"epoch: {enum_word(unit)}", "epoch", f"epoch_unit:{unit}")] if unit else []


def _unmeasured_suffix(stats: dict[str, Any]) -> list[Rendered]:
    """A suffix: which required field this run attempted and could not obtain (SPEC 2.2.4).

    Leads the suffix chain, since it says the fields it names are unknown rather than absent -
    every other absence a reader meets here is a property of the column.
    """

    fields = column_value(stats, "unmeasured")

    return [_field_list("unmeasured", fields)] if fields else []


def _unrepresentable_suffix(stats: dict[str, Any]) -> list[Rendered]:
    """A suffix: which emitted bound falls outside the representable calendar range (SPEC 2.2.4)."""

    fields = column_value(stats, "unrepresentable")

    return [_field_list("unrepresentable", fields)] if fields else []


def _field_list(key: str, fields: list[Any]) -> Rendered:
    listed = LIST_SEPARATOR.join(percentile_label(str(field)) for field in fields)
    percentiles = (percentile_key(str(field)) for field in fields)

    return _fact(f"{key}: {listed}", key, *(p for p in percentiles if p))


def _null_suffix(stats: dict[str, Any], scope: ScanScope | None) -> list[Rendered]:
    share = null_share(stats, scope)

    if share is None:
        return []

    return [
        _fact(
            f"nulls: {share}",
            "nulls",
            *(["over_the_rows_scanned"] if scope and share.startswith("none") else []),
        ),
    ]


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _coverage_method_suffix(stats: dict[str, Any]) -> list[Rendered]:
    """A suffix: `bounded` means the coverage figure itself is a clamp, not a raw measurement.

    Silent on `measured`, exactly as `cardinality_method` stays silent on `exact` (SPEC 2.2.4).
    """

    bounded = column_value(stats, "values_coverage_method") == "bounded"

    return [_fact("coverage bounded", "coverage_bounded")] if bounded else []


def _non_null_rows(
    stats: dict[str, Any],
    row_count: int | None,
    scope: ScanScope | None,
) -> int | None:
    """A part's occurrences, the rows scanned, else the table's row count, less the nulls.

    None where none of them is readable.
    """

    occurrences = column_value(stats, "occurrences")
    population = occurrences if isinstance(occurrences, int) else rows_scanned(stats, scope)
    population = population if isinstance(population, int) else row_count
    nulls = column_value(stats, "null_count")

    if not isinstance(population, int) or isinstance(population, bool):
        return None

    return population - (nulls if isinstance(nulls, int) else 0)


def _non_null(stats: dict[str, Any], row_count: int | None, scope: ScanScope | None) -> int | None:
    """Non-null rows a share is taken against, or None where nothing states them.

    A complete list's sum (SPEC 2.2.5), a truncated one's sum over its coverage, else `_non_null_rows`.
    """

    listed = sum(count for _, count in _value_entries(stats))
    coverage = column_value(stats, "values_coverage")

    if list_is_complete(stats):
        return listed

    if isinstance(coverage, (int, float)) and not isinstance(coverage, bool) and 0 < coverage < 1:
        return max(round(listed / coverage), listed)

    return _non_null_rows(stats, row_count, scope)


def _grouped_entries(
    stats: dict[str, Any],
    entries: list[tuple[Any, int]],
) -> list[tuple[Any, int, int]]:
    """`(value, total, spellings)` per category, members folded into their canonical (SPEC 2.2.4).

    An ungrouped value, or one whose canonical is not listed, counts one spelling.
    """

    groups = grouped_values(column_value(stats, "values") or [])
    members = {value_key(entry.get("value")): spellings for entry, spellings in groups}
    grouped_away = {value_key(m.get("value")) for _, spellings in groups for m in spellings}
    out = []

    for value, count in entries:
        if value_key(value) in grouped_away:
            continue

        spellings = members.get(value_key(value)) or []
        total = count + sum(int(m.get("count") or 0) for m in spellings)
        out.append((value, total, len(spellings) + 1))

    return out


def _spelling_suffix(spellings: int) -> str:
    return f", {spellings} spellings" if spellings > 1 else ""


def _value_entries(stats: dict[str, Any]) -> list[tuple[Any, int]]:
    """Value list as (value, count) pairs, in the order the artifact carries.

    SPEC 2.2.4 orders it by count descending; re-sorting here would hide a wrong producer.
    """

    entries = column_value(stats, "values") or []
    out: list[tuple[Any, int]] = []

    for entry in entries:
        if isinstance(entry, dict):
            out.append((entry.get("value"), int(entry.get("count") or 0)))
        else:
            out.append((getattr(entry, "value", None), int(getattr(entry, "count", 0) or 0)))

    return out


def _value_counts(stats: dict[str, Any]) -> dict[Any, int]:
    """Value list keyed by value, for the boolean pair lookup."""

    return {value: count for value, count in _value_entries(stats)}


def _statistic(value: Any) -> str:
    # A hand-edited print can carry a non-number where a statistic belongs; it is shown, not fatal.
    if isinstance(value, int | float | Decimal) and not isinstance(value, bool):
        return spell_number(value)

    return spell_literal(value)
