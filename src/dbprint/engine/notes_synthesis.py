"""Per-classification "Notes" cell templates for `dbprint context`.

Pure: no I/O, no side effects. SPEC 2.2.2 mandates `classification` on every column dict.
"""

from __future__ import annotations

from typing import Any


TEXT_TOP_VALUES_LIMIT = 2  # values shown once a text column's list is not exhaustive
NULL_RATE_DISPLAY_THRESHOLD = 0.01  # suffix shows only when null_rate >= this


def synthesize(
    column_stats: dict[str, Any],
    fk_target: str | None = None,
    *,
    hints_only: bool = False,
) -> str:
    """Return the Notes cell for one column.

    `column_stats` is the SPEC 2.2.3 column mapping; `fk_target` is
    `<target_table>.<target_column> (<detection>)`, ignored for non-FK classifications.

    `hints_only` keeps only the FK target and the suffixes - candidate key, cluster key,
    shape, sensitivity, epoch unit - for a caller whose own cells already carry the rest.
    """

    classification = column_stats.get("classification", "unsupported")

    if hints_only:
        base = _fk_note(classification, fk_target)
    else:
        redaction = _redaction_primitive(column_stats)
        base = _base_template(classification, column_stats, fk_target, redaction)

    suffix = (
        _candidate_key_suffix(column_stats)
        + _physical_layout_key_suffix(column_stats)
        + _looks_like_suffix(column_stats)
        + _sensitivity_suffix(column_stats)
        + _epoch_unit_suffix(column_stats)
    )

    if not hints_only:
        suffix += _unrepresentable_suffix(column_stats) + _null_rate_suffix(column_stats)

    result = base + suffix

    return result.removeprefix(", ") if hints_only else result


def _base_template(
    classification: str,
    stats: dict[str, Any],
    fk_target: str | None,
    redaction: str | None,
) -> str:
    """Dispatch on classification; the branches that render literals also read the marker.

    Read once here, not inside each helper: a branch that forgets to consult it renders a
    substitution as an observed value. Branches with no cell values are unaffected by
    redaction. Uniqueness is not a classification (SPEC 4.2) and arrives as a suffix.
    """

    if classification == "boolean":
        return _boolean_notes(stats, redaction)

    if classification == "categorical":
        return _categorical_notes(stats, redaction)

    if classification == "foreign_key_candidate":
        return _fk_note(classification, fk_target)

    if classification == "temporal":
        return _temporal_notes(stats, redaction)

    if classification == "numeric":
        return _numeric_notes(stats, redaction)

    if classification == "text":
        return _text_notes(stats, redaction)

    if classification == "json":
        return "json"

    # unsupported and any future fallback
    return stats.get("sql_type", "unsupported")


def _fk_note(classification: str, fk_target: str | None) -> str:
    """The FK branch, shared between the full template and `hints_only` (SPEC 2.3)."""

    if classification != "foreign_key_candidate":
        return ""

    return f"FK -> {fk_target}" if fk_target else "FK candidate"


def _boolean_notes(stats: dict[str, Any], redaction: str | None) -> str:
    """True/false split, or the two group sizes when the labels were withheld.

    Every redaction primitive breaks the pair lookup while leaving counts intact, and
    SPEC 2.2.4 orders by count rather than value, so which size was `true` is unrecoverable.
    """

    if redaction is not None:
        sizes = " / ".join(str(count) for _, count in _value_entries(stats))

        return (
            f"{_redacted_label(redaction)}, counts {sizes}" if sizes else _redacted_label(redaction)
        )

    counts = _value_counts(stats)
    n_true = counts.get(True, counts.get("true", 0))
    n_false = counts.get(False, counts.get("false", 0))

    return f"{n_true} true / {n_false} false"


def _categorical_notes(stats: dict[str, Any], redaction: str | None = None) -> str:
    cardinality = stats.get("cardinality") or 0
    entries = _value_entries(stats)

    # The distinct count is a measurement, not a label, so redaction drops only the values.
    if redaction is not None:
        return f"{cardinality} distinct, {_redacted_label(redaction)}"

    if not entries:
        return f"{cardinality} distinct"

    if _is_exhaustive(stats):
        keys = [_format_value(value) for value, _ in entries]

        return f"{cardinality} distinct: " + " / ".join(keys)

    total = _non_null_total(stats, entries)
    parts = [
        f"{_format_value(value)} ({round(100 * count / total)}%)" for value, count in entries[:3]
    ]

    return f"{cardinality} distinct: " + ", ".join(parts) + f"... ({cardinality} total)"


def _temporal_notes(stats: dict[str, Any], redaction: str | None = None) -> str:
    percentiles = stats.get("percentiles") or {}
    rng = stats.get("range") or {}
    freshness = stats.get("freshness") or {}
    p01 = percentiles.get("p01") or rng.get("min")
    p99 = percentiles.get("p99") or rng.get("max")
    span = rng.get("span_days")
    freshness_class = freshness.get("classification")

    bits = []

    # Bounds carry the substitution; span_days/freshness are derived arithmetic, coarsened
    # rather than substituted (SPEC 2.2.9).
    if redaction is not None:
        bits.append(_redacted_label(redaction))

        if span is not None:
            bits.append(f"{span} day span")
    elif p01 is not None and p99 is not None:
        if span is not None:
            bits.append(f"range {p01} -> {p99} ({span} days)")
        else:
            bits.append(f"range {p01} -> {p99}")

    distribution = stats.get("distribution")

    if distribution:
        bits.append(distribution)

    # No bucket means "not measured", so a column with no `freshness` block gets no verdict.
    if freshness_class:
        bits.append(f"freshness {freshness_class}")

    return ", ".join(bits) if bits else "temporal"


def _numeric_notes(stats: dict[str, Any], redaction: str | None = None) -> str:
    """Range, median and shape; `distribution` survives redaction - a measurement, not a bound."""

    rng = stats.get("range") or {}
    percentiles = stats.get("percentiles") or {}
    mn, mx = rng.get("min"), rng.get("max")
    p50 = percentiles.get("p50")
    distribution = stats.get("distribution")

    if redaction is not None:
        bits = [_redacted_label(redaction)]

        if distribution:
            bits.append(distribution)

        return ", ".join(bits)

    bits = []

    if mn is not None and mx is not None:
        bits.append(f"range {mn}..{mx}")

    if p50 is not None:
        bits.append(f"p50={p50}")

    if distribution:
        bits.append(distribution)

    return ", ".join(bits) if bits else "numeric"


def _text_notes(stats: dict[str, Any], redaction: str | None = None) -> str:
    cardinality = stats.get("cardinality") or 0
    entries = _value_entries(stats)

    if not entries:
        return "text"

    if redaction is not None:
        counts = ", ".join(str(count) for _, count in entries[:TEXT_TOP_VALUES_LIMIT])

        return f"{_redacted_label(redaction)}, top counts {counts}"

    if _is_exhaustive(stats):
        keys = [_format_value(value) for value, _ in entries]

        return f"{cardinality} distinct: " + " / ".join(keys)

    parts = [
        f"{_format_value(value)} ({count})" for value, count in entries[:TEXT_TOP_VALUES_LIMIT]
    ]

    return "top: " + ", ".join(parts)


def _redaction_primitive(stats: dict[str, Any]) -> str | None:
    """The `redacted` marker naming the primitive, or None when the values are real.

    The artifact is hand-editable, so a non-string marker counts as absent rather than
    reaching the cell as though it were a primitive name.
    """

    marker = stats.get("redacted")

    return marker if isinstance(marker, str) and marker else None


def _redacted_label(primitive: str) -> str:
    """How a withheld cell announces itself, naming the primitive the artifact declares."""

    return f"redacted ({primitive})"


def _candidate_key_suffix(stats: dict[str, Any]) -> str:
    """A suffix, not a branch: `inferred.candidate_key` (SPEC 4.2) rides every classification.

    Names the exception when the ratio falls short of 1.0, so the cell does not overclaim.
    """

    inferred = stats.get("inferred") or {}

    if not inferred.get("candidate_key"):
        return ""

    exception = inferred.get("candidate_key_exception")

    if exception is not None:
        return f", candidate key ({exception.replace('_', ' ')})"

    return ", candidate key"


def _physical_layout_key_suffix(stats: dict[str, Any]) -> str:
    """A suffix, not a branch: the marker is orthogonal to every classification above."""

    return ", cluster/partition key" if stats.get("physical_layout_key") else ""


def _looks_like_suffix(stats: dict[str, Any]) -> str:
    """A suffix: the detected shape (SPEC 4.1), present only where SPEC 4.1.5 samples for it."""

    pattern = (stats.get("inferred") or {}).get("looks_like")

    return f", looks like {pattern}" if pattern else ""


def _sensitivity_suffix(stats: dict[str, Any]) -> str:
    """A suffix, a detection and never a verdict: SPEC 4.4 gates redaction, it does not rule."""

    category = (stats.get("inferred") or {}).get("sensitivity")

    return f", {category} detected" if category else ""


def _epoch_unit_suffix(stats: dict[str, Any]) -> str:
    """A suffix: an integer storing a Unix epoch instant rather than a plain quantity (SPEC 4.5)."""

    unit = (stats.get("inferred") or {}).get("epoch_unit")

    return f", epoch ({unit})" if unit else ""


def _unrepresentable_suffix(stats: dict[str, Any]) -> str:
    """A suffix: which emitted bound falls outside the representable calendar range (SPEC 2.2.4)."""

    fields = stats.get("unrepresentable")

    return f", unrepresentable: {', '.join(fields)}" if fields else ""


def _null_rate_suffix(stats: dict[str, Any]) -> str:
    null_rate = stats.get("null_rate", 0.0)

    if not isinstance(null_rate, (int, float)):
        return ""

    if null_rate < NULL_RATE_DISPLAY_THRESHOLD:
        return ""

    pct = round(null_rate * 100, 1)

    if pct == int(pct):
        pct = int(pct)

    return f", {pct}% null"


def _is_exhaustive(stats: dict[str, Any]) -> bool:
    """Whether the emitted value list is the whole domain (SPEC 2.2.4), not a sample of it.

    Exact float compare: `spec.coverage.coverage_share` returns precisely 1.0 for an
    exhaustive list and clamps every truncated one strictly below it.
    """

    return stats.get("values_coverage") == 1.0


def _non_null_total(stats: dict[str, Any], entries: list[tuple[Any, int]]) -> int:
    """Non-null rows the shares are taken against.

    Listed counts add up to the column only when the list is exhaustive, so a truncated one
    recovers the total through its coverage instead.
    """

    listed = sum(count for _, count in entries)
    coverage = stats.get("values_coverage")

    if isinstance(coverage, (int, float)) and not isinstance(coverage, bool) and 0 < coverage < 1:
        return max(round(listed / coverage), listed)

    return listed or 1


def _value_entries(stats: dict[str, Any]) -> list[tuple[Any, int]]:
    """Value list as (value, count) pairs, in the order the artifact carries.

    SPEC 2.2.4 orders it by count descending; re-sorting here would hide a wrong producer.
    """

    entries = stats.get("values") or []
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


def _format_value(value: Any) -> str:
    if value is None:
        return "NULL"
    elif isinstance(value, str):
        return value
    else:
        return str(value)
