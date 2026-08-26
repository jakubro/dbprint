"""Statistics invariants per SPEC 2.2."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dbprint.spec.classification import (
    compute_candidate_key_exception,
    compute_cardinality_ratio,
    compute_null_rate,
    is_candidate_key,
)
from dbprint.spec.coverage import coverage_share, is_incoherent
from dbprint.spec.redaction import REDACTED_DAY_COUNT_GRANULARITY
from dbprint.spec.sketch import METHOD as SKETCH_METHOD
from dbprint.spec.sketch import K as SKETCH_K
from dbprint.spec.sketch import decode_sketch
from dbprint.spec.statistics_matrix import FORBIDDEN_FIELDS as _FORBIDDEN_BY_CLASSIFICATION
from dbprint.spec.statistics_matrix import REQUIRED_FIELDS as _REQUIRED_BY_CLASSIFICATION
from dbprint.spec.temporal_age import day_count, parse_instant
from .issue import Issue


_PRECISION_DECIMALS = 6

# Tolerance for the numeric percentile-containment compare: Snowflake orders percentiles by a
# CAST(... AS DOUBLE) while `range` reads the raw column, and both sides round to six decimals
# independently, so an exact compare can invert containment in the last digit on a correct
# read. Temporal containment needs no tolerance; both sides parse the same ISO text.
_PERCENTILE_CONTAINMENT_TOLERANCE = 1e-06

# The `inferred.*` rows of the SPEC 2.2.3 matrix, kept separate from the flat fields because
# the container's own verdict overrides each sub-field independently; `unsupported` is absent
# since SPEC 2.2.3 forbids the container itself. `epoch_unit` follows SPEC 4.1.5 and SPEC 4.5
# (never epoch-shaped on boolean/json/temporal); `sampled`/`matched` follow `looks_like`,
# whose evidence they are (SPEC 4.1.3). `candidate_key` and `candidate_key_exception` carry
# no row here (SPEC 4.2): `_check_candidate_key` checks them against the measured ratio.
_FORBIDDEN_INFERRED_BY_CLASSIFICATION: dict[str, set[str]] = {
    "boolean": {"looks_like", "sampled", "matched", "fk_candidate", "epoch_unit"},
    "json": {"looks_like", "sampled", "matched", "fk_candidate", "epoch_unit"},
    "categorical": {"fk_candidate"},
    "temporal": {"looks_like", "sampled", "matched", "fk_candidate", "epoch_unit"},
    "numeric": {"looks_like", "sampled", "matched", "fk_candidate"},
    "text": {"fk_candidate"},
}


@dataclass(frozen=True)
class _ConditionalCell:
    """A SPEC 2.2.3 cell whose verdict depends on another field of the same column.

    The matrix is otherwise a function of `classification` alone. A registered cell is REQUIRED
    while its condition is unmet and FORBIDDEN once it holds. `holds` reads the whole column,
    not one named field: a redaction is a top-level marker, a shape sits under `inferred`.
    """

    classifications: frozenset[str]
    fields: frozenset[str]
    reason: str
    spec_ref: str
    holds: Callable[[dict], bool]


def _inferred_of(col: dict) -> dict:
    """The column's `inferred` sub-object, or an empty one when it has none."""

    value = col.get("inferred")

    return value if isinstance(value, dict) else {}


_CONDITIONAL_CELLS: tuple[_ConditionalCell, ...] = (
    # `drop` emits no literal, so the bound fields are absent, not placeholders (SPEC 2.2.9).
    # `freshness` is unaffected: `max_age_days` is derived, not a value read from a cell.
    _ConditionalCell(
        classifications=frozenset({"numeric", "temporal"}),
        fields=frozenset({"range", "percentiles"}),
        reason="the column declares redacted: drop, which emits no literal",
        spec_ref="§2.2.9",
        holds=lambda col: col.get("redacted") == "drop",
    ),
    # Prose top values are unusable and their grouped scan is expensive, so the format
    # exempts the enumeration (and `distribution`, derived from it) for `text` alone.
    _ConditionalCell(
        classifications=frozenset({"text"}),
        fields=frozenset({"values", "values_coverage", "distribution"}),
        reason="the column is inferred prose, which publishes no value list",
        spec_ref="§2.2.3",
        holds=lambda col: _inferred_of(col).get("looks_like") == "prose",
    ),
)


def check(data: Any, path: str, tbl_fqn: str) -> list[Issue]:
    """Check one statistics.yaml body beyond what the JSON Schema covers."""

    if not isinstance(data, dict):
        return []

    issues: list[Issue] = []
    row_count = data.get("row_count", 0)
    columns = data.get("columns", {})
    profiled_at = data.get("profiled_at")
    # SPEC 2.2.15: no query was issued at all - the schema's own CatalogOnlyColumn
    # definition governs each column's field set, so the SPEC 2.2.3 matrix does not apply.
    catalog_only = data.get("catalog_only") is True

    issues.extend(_check_scope(data, path, row_count))

    # SPEC 2.2.8 counts over the scanned set: the whole table only when `scope` is absent.
    scoped = isinstance(data.get("scope"), dict)
    rows_scanned = _rows_scanned(data, row_count)

    if not isinstance(columns, dict):
        return issues

    issues.extend(_check_null_patterns(data, path, columns, rows_scanned))
    issues.extend(_check_physical_layout(data, path, columns))
    issues.extend(_check_grain(data, path, columns, row_count, scoped))
    issues.extend(_check_dependencies(data, path, columns, row_count, scoped))
    issues.extend(_check_catalog_only_columns(columns, path, catalog_only=catalog_only))

    for col_name, col in columns.items():
        if not isinstance(col, dict):
            continue

        col_path = f"{path}::columns.{col_name}"
        classification = col.get("classification")

        if classification in _REQUIRED_BY_CLASSIFICATION and not catalog_only:
            issues.extend(_check_matrix(col, col_path, classification))

        issues.extend(_check_count_invariants(col, col_path, rows_scanned))
        issues.extend(_check_candidate_key(col, col_path, rows_scanned))
        issues.extend(
            _check_population_marker(col, col_path, scoped=scoped, rows_scanned=rows_scanned),
        )
        issues.extend(_check_value_order(col, col_path))
        issues.extend(_check_redaction_marker(col, col_path))
        issues.extend(_check_unredacted_sensitive(col, col_path))
        issues.extend(_check_distribution(col, col_path))
        issues.extend(_check_frequencies_distribution(col, col_path, rows_scanned))
        issues.extend(_check_precision(col, col_path))
        issues.extend(_check_unrepresentable(col, col_path))
        issues.extend(_check_span_days(col, col_path))
        issues.extend(_check_percentiles_order(col, col_path))
        issues.extend(_check_percentiles_containment(col, col_path))
        issues.extend(_check_max_age_days_mismatch(col, col_path, profiled_at))
        issues.extend(_check_redacted_day_counts(col, col_path))
        issues.extend(_check_physical_name(col, col_path, col_name))
        issues.extend(_check_sketch(col, col_path))

    return issues


def _check_null_patterns(
    data: dict,
    path: str,
    columns: dict,
    rows_scanned: int,
) -> list[Issue]:
    """Validate the table-level null census per SPEC 2.2.10.

    Summing the pattern counts that name a column must reproduce that column's own
    `null_count` - the format's only arithmetic identity crossing from a table-level object
    to a per-column one, which two figures read over different scans cannot both satisfy.
    """

    block = data.get("null_patterns")
    nulls_exist = any(
        isinstance(col, dict) and isinstance(col.get("null_count"), int) and col["null_count"] > 0
        for col in columns.values()
    )

    if block is None or not isinstance(block, dict):
        if nulls_exist:
            return [
                Issue(
                    path,
                    "stats.null-patterns-absent-with-nulls",
                    "error",
                    "a column carries nulls but no `null_patterns` block says which columns "
                    "carry them together; an absent block asserts the table has no nulls",
                    "§2.2.10",
                ),
            ]

        return []

    issues: list[Issue] = []

    if not nulls_exist:
        issues.append(
            Issue(
                path,
                "stats.null-patterns-absent-with-nulls",
                "error",
                "a `null_patterns` block is present but no column reports a null; the block "
                "is emitted only when some column carries one",
                "§2.2.10",
            ),
        )

    patterns = block.get("patterns")

    if not isinstance(patterns, list):
        return issues

    entries = [p for p in patterns if isinstance(p, dict)]
    issues.extend(_check_null_pattern_columns(entries, path, columns))
    issues.extend(_check_null_pattern_distinctness(entries, path))
    issues.extend(_check_null_pattern_order(entries, path))
    issues.extend(_check_null_pattern_totals(entries, block, path, rows_scanned))

    return issues + _check_null_pattern_reconciliation(entries, block, path, columns)


def _check_physical_layout(data: dict, path: str, columns: dict) -> list[Issue]:
    """SPEC 2.2.11: a named `column` must exist, and the two surfaces must agree.

    `physical_layout.keys[].column` and each column's own `physical_layout_key` are two views
    of one fact, so emitting one without the other writes a file that contradicts itself.
    """

    block = data.get("physical_layout")

    if not isinstance(block, dict):
        return []

    keys = [k for k in block.get("keys") or [] if isinstance(k, dict)]
    declared = {k["column"] for k in keys if isinstance(k.get("column"), str)}
    unknown = sorted(name for name in declared if name not in columns)
    issues: list[Issue] = []

    if unknown:
        issues.append(
            Issue(
                path,
                "stats.physical-layout-unknown-column",
                "error",
                f"physical_layout.keys names column(s) {unknown} not present in `columns`.",
                "§2.2.11",
            ),
        )

    marked = {
        name
        for name, col in columns.items()
        if isinstance(col, dict) and col.get("physical_layout_key") is True
    }
    undeclared = sorted(marked - declared)
    unmarked = sorted(declared - marked)

    if undeclared:
        issues.append(
            Issue(
                path,
                "stats.physical-layout-key-not-declared",
                "error",
                f"column(s) {undeclared} carry `physical_layout_key: true` but are not named "
                "in physical_layout.keys.",
                "§2.2.11",
            ),
        )

    if unmarked:
        issues.append(
            Issue(
                path,
                "stats.physical-layout-key-missing-marker",
                "error",
                f"physical_layout.keys names column(s) {unmarked} that do not carry "
                "`physical_layout_key: true`.",
                "§2.2.11",
            ),
        )

    return issues


def _check_grain(
    data: dict,
    path: str,
    columns: dict,
    row_count: int,
    scoped: bool,
) -> list[Issue]:
    """SPEC 2.2.12: every named column exists, no entry repeats, and a measured key never
    lands where a sample or an empty table would overclaim uniqueness.

    Does not parse `ddl.sql` to cross-check a `declared` entry; SPEC 2.6.7 calls it brittle.
    """

    block = data.get("grain")

    if not isinstance(block, dict):
        return []

    keys = [k for k in block.get("keys") or [] if isinstance(k, dict)]
    unknown: set[str] = set()
    seen: set[frozenset[str]] = set()
    duplicated: set[frozenset[str]] = set()
    measured_present = False

    for key in keys:
        cols = key.get("columns")

        if not isinstance(cols, list):
            continue

        names = tuple(c for c in cols if isinstance(c, str))
        unknown.update(name for name in names if name not in columns)
        # A duplicate is the same column SET twice: order matters to a declared key's own
        # encoding (SPEC 2.2.12), but two entries over the same columns are redundant anyway.
        combination = frozenset(names)

        if combination in seen:
            duplicated.add(combination)

        seen.add(combination)

        if key.get("detection") == "measured":
            measured_present = True

    issues: list[Issue] = []

    if unknown:
        issues.append(
            Issue(
                path,
                "stats.grain-unknown-column",
                "error",
                f"grain.keys names column(s) {sorted(unknown)} not present in `columns`.",
                "§2.2.12",
            ),
        )

    if duplicated:
        issues.append(
            Issue(
                path,
                "stats.grain-duplicate-key",
                "error",
                f"grain.keys lists {sorted(list(c) for c in duplicated)} more than once.",
                "§2.2.12",
            ),
        )

    if measured_present and scoped:
        issues.append(
            Issue(
                path,
                "stats.grain-measured-under-scope",
                "error",
                "grain.keys carries a `measured` entry on a file that also carries `scope`; "
                "uniqueness over a sample is not uniqueness.",
                "§2.2.12",
            ),
        )

    if measured_present and row_count == 0:
        issues.append(
            Issue(
                path,
                "stats.grain-measured-on-empty-table",
                "error",
                "grain.keys carries a `measured` entry on a table with row_count 0; every "
                "combination is trivially unique there and MUST be excluded.",
                "§2.2.12",
            ),
        )

    return issues


def _check_dependencies(
    data: dict,
    path: str,
    columns: dict,
    row_count: int,
    scoped: bool,
) -> list[Issue]:
    """SPEC 2.2.13: every named column exists, strength is in range, cardinality permits the
    direction, and no measurement lands where a sample or an empty table would overclaim.
    """

    entries = [e for e in data.get("dependencies") or [] if isinstance(e, dict)]

    if not entries:
        return []

    issues: list[Issue] = []
    unknown: set[str] = set()
    self_referential = 0
    out_of_range = 0
    direction_violations = 0

    for entry in entries:
        determinant, dependent = entry.get("determinant"), entry.get("dependent")

        if isinstance(determinant, str) and determinant not in columns:
            unknown.add(determinant)

        if isinstance(dependent, str) and dependent not in columns:
            unknown.add(dependent)

        if determinant is not None and determinant == dependent:
            self_referential += 1

        strength = entry.get("strength")

        if not isinstance(strength, (int, float)) or not 0 < strength <= 1:
            out_of_range += 1

        # SPEC 2.2.13: cardinality(determinant) >= cardinality(dependent) is necessary for
        # the direction to be possible - a function's image has no more distinct values than
        # its domain. Read independent of `strength`: an exact claim is as checkable as a
        # partial one.
        det_col = columns.get(determinant) if isinstance(determinant, str) else None
        dep_col = columns.get(dependent) if isinstance(dependent, str) else None
        det_cardinality = det_col.get("cardinality") if isinstance(det_col, dict) else None
        dep_cardinality = dep_col.get("cardinality") if isinstance(dep_col, dict) else None

        if (
            isinstance(det_cardinality, int)
            and isinstance(dep_cardinality, int)
            and det_cardinality < dep_cardinality
        ):
            direction_violations += 1

    if unknown:
        issues.append(
            Issue(
                path,
                "stats.dependencies-unknown-column",
                "error",
                f"dependencies names column(s) {sorted(unknown)} not present in `columns`.",
                "§2.2.13",
            ),
        )

    if self_referential:
        issues.append(
            Issue(
                path,
                "stats.dependencies-self-referential",
                "error",
                f"dependencies carries {self_referential} entry(ies) whose determinant and "
                f"dependent name the same column.",
                "§2.2.13",
            ),
        )

    if out_of_range:
        issues.append(
            Issue(
                path,
                "stats.dependencies-strength-out-of-range",
                "error",
                f"dependencies carries {out_of_range} entry(ies) whose strength is not in (0, 1].",
                "§2.2.13",
            ),
        )

    if direction_violations:
        issues.append(
            Issue(
                path,
                "stats.dependencies-direction-impossible",
                "error",
                f"dependencies carries {direction_violations} entry(ies) whose determinant has "
                f"lower cardinality than its dependent - impossible for that direction.",
                "§2.2.13",
            ),
        )

    if scoped:
        issues.append(
            Issue(
                path,
                "stats.dependencies-measured-under-scope",
                "error",
                "dependencies is non-empty on a file that also carries `scope`; a dependency "
                "measured over a sample is not a dependency.",
                "§2.2.13",
            ),
        )

    if row_count == 0:
        issues.append(
            Issue(
                path,
                "stats.dependencies-measured-on-empty-table",
                "error",
                "dependencies is non-empty on a table with row_count 0; every combination is "
                "trivially functional there and MUST be excluded.",
                "§2.2.13",
            ),
        )

    return issues


def _check_null_pattern_columns(entries: list[dict], path: str, columns: dict) -> list[Issue]:
    """Every named column exists in this file, so a pattern is readable on its own."""

    unknown = sorted(
        {name for entry in entries for name in _pattern_columns(entry) if name not in columns},
    )

    if not unknown:
        return []

    return [
        Issue(
            path,
            "stats.null-patterns-unknown-column",
            "error",
            f"`null_patterns` names {unknown}, absent from this file's `columns` map",
            "§2.2.10",
        ),
    ]


def _check_null_pattern_distinctness(entries: list[dict], path: str) -> list[Issue]:
    """Each combination appears once, or the entries do not partition anything.

    Two entries naming the same columns double-count the rows they describe and break the
    reconciliation, without either count being wrong on its own.
    """

    seen = [_pattern_columns(entry) for entry in entries]
    repeated = sorted({combination for combination in seen if seen.count(combination) > 1})

    if not repeated:
        return []

    return [
        Issue(
            path,
            "stats.null-patterns-duplicate-combination",
            "error",
            f"`null_patterns` lists {[list(c) for c in repeated]} more than once; entries "
            f"partition the rows they cover, so each combination appears at most once",
            "§2.2.10",
        ),
    ]


def _check_null_pattern_order(entries: list[dict], path: str) -> list[Issue]:
    """SPEC 2.2.10: count descending, ties by ascending column-name array."""

    keys = [(-_pattern_count(entry), _pattern_columns(entry)) for entry in entries]

    if keys == sorted(keys):
        return []

    return [
        Issue(
            path,
            "stats.null-patterns-not-ordered",
            "error",
            "`null_patterns.patterns` is not ordered by `count` descending with ties broken "
            "by ascending `columns`",
            "§2.2.10",
        ),
    ]


def _check_null_pattern_totals(
    entries: list[dict],
    block: dict,
    path: str,
    rows_scanned: int,
) -> list[Issue]:
    """The listed counts fit inside the scanned rows, and `coverage` is their share."""

    issues: list[Issue] = []
    listed = sum(_pattern_count(entry) for entry in entries)

    if listed > rows_scanned:
        detail = (
            f"`null_patterns` counts sum to {listed} over {rows_scanned} rows scanned; "
            f"the entries partition the rows they cover, so they cannot exceed them"
        )

        # `bounded` (SPEC 2.2.10) is the producer disclosing that the census and rows_scanned
        # were not read at the same instant - the disagreement here is already named.
        if block.get("coverage_method") == "bounded":
            issues.append(
                Issue(
                    path,
                    "stats.null-patterns-sum-exceeds-rows-scanned-bounded",
                    "warning",
                    detail,
                    "§2.2.10",
                ),
            )
        else:
            issues.append(
                Issue(
                    path,
                    "stats.null-patterns-sum-exceeds-rows-scanned",
                    "error",
                    detail,
                    "§2.2.10",
                ),
            )

    coverage = block.get("coverage")

    if not isinstance(coverage, (int, float)) or isinstance(coverage, bool):
        return issues

    # Recomputed through `coverage_share`, which clamps a truncated census below 1.0, so a
    # census whose omitted tail rounds away is not rejected as its producer's own error. The
    # clamp hides nothing: an incoherent read is checked above, against the operands.
    expected = coverage_share(listed, rows_scanned, exhaustive=_is_exhaustive(coverage))

    if abs(coverage - expected) > 1e-06:
        issues.append(
            Issue(
                path,
                "stats.null-patterns-coverage-mismatch",
                "error",
                f"`null_patterns.coverage` is {coverage} but the listed counts ({listed}) over "
                f"the rows scanned ({rows_scanned}) give {expected}",
                "§2.2.10",
            ),
        )

    return issues


def _check_null_pattern_reconciliation(
    entries: list[dict],
    block: dict,
    path: str,
    columns: dict,
) -> list[Issue]:
    """Per column, the patterns naming it account for at most its own `null_count`.

    Exactly its `null_count` at `coverage: 1.0`, where every row is described by some entry.
    """

    coverage = block.get("coverage")
    complete = (
        isinstance(coverage, (int, float)) and not isinstance(coverage, bool) and coverage == 1.0
    )
    implied: dict[str, int] = {}

    for entry in entries:
        for name in _pattern_columns(entry):
            implied[name] = implied.get(name, 0) + _pattern_count(entry)

    issues: list[Issue] = []

    for name, col in columns.items():
        if not isinstance(col, dict) or not isinstance(col.get("null_count"), int):
            continue

        null_count = col["null_count"]
        counted = implied.get(name, 0)

        if counted > null_count or (complete and counted != null_count):
            detail = (
                f"the `null_patterns` entries naming {name!r} account for {counted} null "
                f"rows, but the column reports null_count {null_count}"
            )

            # `bounded` (SPEC 2.2.10) is the producer disclosing that the census and this
            # column's null_count were not read at the same instant.
            if block.get("coverage_method") == "bounded":
                issues.append(
                    Issue(
                        path,
                        "stats.null-patterns-reconciliation-mismatch-bounded",
                        "warning",
                        detail,
                        "§2.2.10",
                    ),
                )
            else:
                issues.append(
                    Issue(
                        path,
                        "stats.null-patterns-reconciliation-mismatch",
                        "error",
                        detail,
                        "§2.2.10",
                    ),
                )

    return issues


def _pattern_columns(entry: dict) -> tuple[str, ...]:
    names = entry.get("columns")

    return tuple(n for n in names if isinstance(n, str)) if isinstance(names, list) else ()


def _pattern_count(entry: dict) -> int:
    count = entry.get("count")

    return count if isinstance(count, int) and not isinstance(count, bool) else 0


def _rows_scanned(data: dict, row_count: int) -> int:
    """Rows the statistics were measured over; the table's own count by default."""

    scope = data.get("scope")

    if not isinstance(scope, dict):
        return row_count

    value = scope.get("rows_scanned")

    return value if isinstance(value, int) else row_count


def _check_scope(data: dict, path: str, row_count: int) -> list[Issue]:
    """Validate the optional subset-provenance block per SPEC 2.2.8.

    `rows_scanned` above an exact `row_count` is impossible and reported; above an estimated
    one it is a lagging estimate, which SPEC 2.2.8 sanctions.
    """

    scope = data.get("scope")

    if scope is None:
        return []

    scope_path = f"{path}::scope"

    if not isinstance(scope, dict):
        return [
            Issue(
                scope_path,
                "stats.scope-not-a-mapping",
                "error",
                f"scope must be a mapping, got {type(scope).__name__}.",
                "§2.2.8",
            ),
        ]

    issues: list[Issue] = []
    rows_scanned = scope.get("rows_scanned")
    sample = scope.get("sample")
    has_filter = isinstance(scope.get("filter"), str)
    # Only an explicit `approximate` relaxes the rule; missing or unrecognized does not.
    row_count_method = data.get("row_count_method")

    if not isinstance(rows_scanned, int):
        issues.append(
            Issue(
                scope_path,
                "stats.scope-missing-rows-scanned",
                "error",
                "scope is present but carries no integer rows_scanned.",
                "§2.2.8",
            ),
        )
    elif rows_scanned > row_count and row_count_method != "approximate":
        issues.append(
            Issue(
                scope_path,
                "stats.scope-rows-scanned-exceeds-row-count",
                "error",
                f"rows_scanned={rows_scanned} exceeds row_count={row_count} and "
                f"row_count_method is {row_count_method!r}; a subset cannot be larger than the "
                f"set unless the set was estimated.",
                "§2.2.8",
            ),
        )

    if sample is not None and (not isinstance(sample, (int, float)) or not 0 < sample <= 1):
        issues.append(
            Issue(
                scope_path,
                "stats.scope-sample-out-of-range",
                "error",
                f"sample={sample!r} is outside the interval (0, 1].",
                "§2.2.8",
            ),
        )

    # Presence, not validity: two narrowing keys is the violation, whatever they hold.
    if "sample" in scope and "filter" in scope:
        issues.append(
            Issue(
                scope_path,
                "stats.scope-sample-and-filter",
                "error",
                "scope carries both sample and filter; a table is narrowed by a predicate or "
                "by a fraction, never both.",
                "§2.2.8",
            ),
        )

    if rows_scanned == row_count and sample is None and not has_filter:
        issues.append(
            Issue(
                scope_path,
                "stats.scope-asserts-nothing",
                "warning",
                "scope covers the whole table and records neither sample nor filter; omit it.",
                "§2.2.8",
            ),
        )

    return issues


# Catalog/DDL-derivable per-column fields - never a query result. Mirrors the schema's own
# CatalogOnlyColumn definition, so the two cannot disagree about what is permitted.
_CATALOG_ONLY_COLUMN_FIELDS = frozenset(
    {"sql_type", "nullable", "classification", "physical_name", "collation", "physical_layout_key"},
)


def _check_catalog_only_columns(
    columns: dict,
    path: str,
    *,
    catalog_only: bool,
) -> list[Issue]:
    """SPEC 2.2.15: a file stating no query was issued may not publish a measurement.

    The schema's CatalogOnlyColumn already forbids these structurally; this names the
    offending column and field, which a bare schema violation does not.
    """

    if not catalog_only:
        return []

    issues: list[Issue] = []

    for col_name, col in columns.items():
        if not isinstance(col, dict):
            continue

        measured = sorted(set(col) - _CATALOG_ONLY_COLUMN_FIELDS)

        if measured:
            issues.append(
                Issue(
                    f"{path}::columns.{col_name}",
                    "stats.measurement-under-catalog-only",
                    "error",
                    f"catalog_only states no query was issued, but this column carries "
                    f"{', '.join(measured)} - a measurement.",
                    "§2.2.15",
                ),
            )

    return issues


def _check_matrix(col: dict, col_path: str, classification: str) -> list[Issue]:
    issues: list[Issue] = []
    required, forbidden, exceptions = _matrix_cells(col, classification)

    for field in required:
        if field not in col:
            issues.append(
                Issue(
                    col_path,
                    "stats.missing-required-field-for-classification",
                    "error",
                    f"Column with classification={classification!r} is missing required field {field!r}.",
                    "§2.2.3",
                ),
            )

    for field in forbidden:
        if field in col:
            cell = exceptions.get(field)
            issues.append(
                Issue(
                    col_path,
                    "stats.forbidden-field-for-classification",
                    "error",
                    f"Column with classification={classification!r} MUST NOT emit field {field!r}"
                    + (f", since {cell.reason}." if cell is not None else "."),
                    cell.spec_ref if cell is not None else "§2.2.3",
                ),
            )

    issues.extend(_check_inferred_matrix(col, col_path, classification))

    return issues


def _matrix_cells(
    col: dict,
    classification: str,
) -> tuple[set[str], set[str], dict[str, _ConditionalCell]]:
    """The required and forbidden field sets for one column, exceptions applied.

    Each moved field carries the exception that moved it, so a rejection names the condition.
    """

    required = set(_REQUIRED_BY_CLASSIFICATION[classification])
    forbidden = set(_FORBIDDEN_BY_CLASSIFICATION[classification])
    exceptions: dict[str, _ConditionalCell] = {}

    for cell in _CONDITIONAL_CELLS:
        if classification not in cell.classifications or not cell.holds(col):
            continue

        required -= cell.fields
        forbidden |= cell.fields
        exceptions.update(dict.fromkeys(cell.fields, cell))

    return required, forbidden, exceptions


def _check_inferred_matrix(col: dict, col_path: str, classification: str) -> list[Issue]:
    """Check the `inferred.*` forbidden rows of the matrix, which a flat key test cannot reach.

    Reported under the same code as the flat rows, with the dotted name in the detail. An
    absent or malformed container is the flat check's business. No classification REQUIRES an
    `inferred.*` sub-field.
    """

    inferred = col.get("inferred")

    if not isinstance(inferred, dict):
        return []

    return [
        Issue(
            col_path,
            "stats.forbidden-field-for-classification",
            "error",
            f"Column with classification={classification!r} MUST NOT emit field 'inferred.{name}'.",
            "§2.2.3",
        )
        for name in sorted(_FORBIDDEN_INFERRED_BY_CLASSIFICATION.get(classification, set()))
        if name in inferred
    ]


def _check_candidate_key(col: dict, col_path: str, rows_scanned: int) -> list[Issue]:
    """SPEC 4.2: `candidate_key` and `candidate_key_exception` must agree with the ratio.

    Independent of classification: it applies to every column carrying a cardinality.
    """

    cardinality = col.get("cardinality")
    cardinality_ratio = col.get("cardinality_ratio")
    cardinality_method = col.get("cardinality_method")
    null_count = col.get("null_count")

    if not (
        isinstance(cardinality, int)
        and isinstance(cardinality_ratio, (int, float))
        and not isinstance(cardinality_ratio, bool)
        and isinstance(cardinality_method, str)
        and isinstance(null_count, int)
    ):
        return []

    inferred = _inferred_of(col)
    expected_key = is_candidate_key(cardinality, float(cardinality_ratio)) or None
    observed_key = inferred.get("candidate_key")
    issues: list[Issue] = []

    if observed_key != expected_key:
        issues.append(
            Issue(
                col_path,
                "stats.candidate-key-mismatch",
                "error",
                f"inferred.candidate_key={observed_key!r} disagrees with the recomputed "
                f"value {expected_key!r} for cardinality_ratio={cardinality_ratio!r}.",
                "§4.2",
            ),
        )

    expected_exception = (
        compute_candidate_key_exception(
            cardinality,
            float(cardinality_ratio),
            cardinality_method,
            rows_scanned,
            null_count,
        )
        if expected_key
        else None
    )
    observed_exception = inferred.get("candidate_key_exception")

    if observed_exception != expected_exception:
        issues.append(
            Issue(
                col_path,
                "stats.candidate-key-exception-mismatch",
                "error",
                f"inferred.candidate_key_exception={observed_exception!r} disagrees with the "
                f"recomputed value {expected_exception!r}.",
                "§4.2",
            ),
        )

    return issues


def _check_population_marker(
    col: dict,
    col_path: str,
    *,
    scoped: bool,
    rows_scanned: int,
) -> list[Issue]:
    """SPEC 2.2.8: every column echoes `rows_scanned` when the file is scoped, never otherwise.

    Presence is decided by the file's own `scope` block, not by `classification`.
    """

    marker = col.get("rows_scanned")
    expected = rows_scanned if scoped else None

    if marker != expected:
        return [
            Issue(
                col_path,
                "stats.population-marker-mismatch",
                "error",
                f"rows_scanned={marker!r} but the file's scope requires {expected!r}.",
                "§2.2.8",
            ),
        ]

    return []


def _check_count_invariants(col: dict, col_path: str, rows_scanned: int) -> list[Issue]:
    issues: list[Issue] = []
    null_count = col.get("null_count")
    cardinality = col.get("cardinality")
    values = col.get("values")

    if isinstance(null_count, int) and null_count > rows_scanned:
        issues.append(
            Issue(
                col_path,
                "stats.null-count-exceeds-row-count",
                "error",
                f"null_count={null_count} exceeds the scanned row count {rows_scanned}.",
                "§2.2.7",
            ),
        )

    if (
        isinstance(cardinality, int)
        and isinstance(null_count, int)
        and cardinality > max(rows_scanned - null_count, 0)
    ):
        issues.append(
            Issue(
                col_path,
                "stats.cardinality-exceeds-row-count",
                "error",
                f"cardinality={cardinality} exceeds the non-null scanned count "
                f"{rows_scanned - null_count}.",
                "§2.2.7",
            ),
        )

    # Recomputed through the producer's own pure-arithmetic rules. Unlike values_coverage
    # neither rule clamps: the impossible input (a count exceeding rows_scanned) is caught
    # above, so there is no operand check to read around.
    if isinstance(null_count, int):
        null_rate = col.get("null_rate")

        if isinstance(null_rate, (int, float)) and not isinstance(null_rate, bool):
            expected_rate = compute_null_rate(null_count, rows_scanned)

            if abs(expected_rate - float(null_rate)) > 1e-06:
                issues.append(
                    Issue(
                        col_path,
                        "stats.null-rate-mismatch",
                        "error",
                        f"null_rate={null_rate} disagrees with null_count={null_count} of "
                        f"rows_scanned={rows_scanned} ({expected_rate}).",
                        "§2.2.6",
                    ),
                )

    if isinstance(cardinality, int):
        cardinality_ratio = col.get("cardinality_ratio")

        if isinstance(cardinality_ratio, (int, float)) and not isinstance(cardinality_ratio, bool):
            expected_ratio = compute_cardinality_ratio(cardinality, rows_scanned)

            if abs(expected_ratio - float(cardinality_ratio)) > 1e-06:
                issues.append(
                    Issue(
                        col_path,
                        "stats.cardinality-ratio-mismatch",
                        "error",
                        f"cardinality_ratio={cardinality_ratio} disagrees with "
                        f"cardinality={cardinality} of rows_scanned={rows_scanned} "
                        f"({expected_ratio}).",
                        "§2.2.6",
                    ),
                )

    if isinstance(values, list) and isinstance(null_count, int):
        listed = _listed_total(values)
        non_null = max(rows_scanned - null_count, 0)
        coverage = col.get("values_coverage")
        exhaustive = _is_exhaustive(coverage)

        # An exhaustive list carries the whole column, so its counts must add up.
        if exhaustive and listed != non_null:
            issues.append(
                Issue(
                    col_path,
                    "stats.values-sum-mismatch",
                    "warning",
                    f"values_coverage is 1.0 but the listed counts ({listed}) do not equal "
                    f"the non-null scanned count ({non_null}).",
                    "§2.2.4",
                ),
            )
        elif is_incoherent(listed, non_null):
            # `coverage_share` clamps this case to TRUNCATED_CLAMP, so the comparison below
            # never sees it; the operands are checked directly instead of the clamped output.
            issues.append(
                Issue(
                    col_path,
                    "stats.values-sum-mismatch",
                    "warning",
                    f"listed value counts ({listed}) exceed the non-null scanned count "
                    f"({non_null}); values and null_count were read in separate statements.",
                    "§2.2.4",
                ),
            )

        if isinstance(coverage, (int, float)) and not isinstance(coverage, bool) and non_null:
            # Recomputed via coverage_share, so the compare uses the producer's own clamped rule.
            expected = coverage_share(listed, non_null, exhaustive=exhaustive)

            if abs(expected - float(coverage)) > 1e-06:
                issues.append(
                    Issue(
                        col_path,
                        "stats.values-coverage-mismatch",
                        "warning",
                        f"values_coverage={coverage} disagrees with the listed counts "
                        f"({listed} of {non_null} = {expected}).",
                        "§2.2.4",
                    ),
                )

        # An exhaustive list carries every distinct non-null value, so its entry count must
        # equal cardinality; gated on `exact`, since an estimate may legitimately disagree.
        if (
            exhaustive
            and isinstance(cardinality, int)
            and col.get("cardinality_method") == "exact"
            and len(values) != cardinality
        ):
            detail = (
                f"the values list carries {len(values)} entries but cardinality is "
                f"{cardinality}; an exhaustive list must carry exactly cardinality entries."
            )

            if len(values) < cardinality:
                # `bounded` (SPEC 2.2.4) is the producer disclosing that values and cardinality
                # were not read at the same instant - the disagreement here is already named.
                if col.get("values_coverage_method") == "bounded":
                    issues.append(
                        Issue(
                            col_path,
                            "stats.values-list-short-of-cardinality-bounded",
                            "warning",
                            detail,
                            "§2.2.4",
                        ),
                    )
                else:
                    issues.append(
                        Issue(
                            col_path,
                            "stats.values-list-short-of-cardinality",
                            "error",
                            detail,
                            "§2.2.4",
                        ),
                    )
            else:
                issues.append(
                    Issue(
                        col_path,
                        "stats.values-list-exceeds-cardinality",
                        "warning",
                        detail,
                        "§2.2.4",
                    ),
                )

    return issues


def _check_value_order(col: dict, col_path: str) -> list[Issue]:
    """Verify the SPEC 2.2.4 ordering: count DESC, lexicographic tie-break."""

    values = col.get("values")

    if not isinstance(values, list) or len(values) < 2:
        return []

    # Redaction leaves no literal for the SPEC 2.2.4 tie-break; only count ordering is checked.
    redacted = col.get("redacted") is not None
    keys = [
        (-int(e["count"]),) if redacted else (-int(e["count"]), str(e.get("value")))
        for e in values
        if isinstance(e, dict) and isinstance(e.get("count"), int)
    ]

    if len(keys) != len(values) or keys == sorted(keys):
        return []

    return [
        Issue(
            col_path,
            "stats.values-not-ordered",
            "error",
            "values is not ordered by count descending with a lexicographic tie-break.",
            "§2.2.4",
        ),
    ]


def _listed_total(values: list) -> int:
    return sum(
        int(e["count"]) for e in values if isinstance(e, dict) and isinstance(e.get("count"), int)
    )


def _is_exhaustive(coverage: object) -> bool:
    """True when the list claims to carry the whole column."""

    return isinstance(coverage, (int, float)) and not isinstance(coverage, bool) and coverage == 1.0


def _check_distribution(col: dict, col_path: str) -> list[Issue]:
    """Verify distribution against an exhaustive `values` list, when verifiable.

    A truncated list shows the minimum frequency of a prefix, not the column's.
    """

    distribution = col.get("distribution")
    values = col.get("values")

    if not (isinstance(distribution, str) and isinstance(values, list) and values):
        return []

    if not _is_exhaustive(col.get("values_coverage")):
        return []

    counts = [
        int(e["count"])
        for e in values
        if isinstance(e, dict) and isinstance(e.get("count"), int) and e["count"] >= 0
    ]

    if not counts:
        return []

    total = sum(counts)

    if total == 0:
        return []

    top = max(counts)
    bot = min(counts) if min(counts) > 0 else 1
    expected = _distribution_for_categorical(top, bot, total)

    if distribution != expected:
        return [
            Issue(
                col_path,
                "stats.distribution-mismatch",
                "warning",
                f"distribution={distribution!r} disagrees with the value list; "
                f"expected {expected!r}.",
                "§2.2.5",
            ),
        ]

    return []


def _check_frequencies_distribution(col: dict, col_path: str, rows_scanned: int) -> list[Issue]:
    """Verify `distribution` against `frequencies` on numeric/temporal columns.

    Those two carry no `values` list for `_check_distribution` to recompute from. Exact rather
    than heuristic - `frequencies` comes from the same top-N fetch the verdict does - hence the
    error severity. Only runs where `cardinality` is exact.
    """

    distribution = col.get("distribution")
    frequencies = col.get("frequencies")
    null_count = col.get("null_count")
    cardinality = col.get("cardinality")

    if not (isinstance(distribution, str) and isinstance(frequencies, dict)):
        return []

    if col.get("cardinality_method") != "exact":
        return []

    if not isinstance(null_count, int) or isinstance(null_count, bool):
        return []

    if not isinstance(cardinality, int) or isinstance(cardinality, bool):
        return []

    counts = [frequencies.get(k) for k in ("top", "bottom", "listed", "total")]

    if not all(isinstance(c, int) and not isinstance(c, bool) for c in counts):
        return []

    top, bottom, listed, total = counts
    non_null = max(rows_scanned - null_count, 0)
    exhaustive = listed == cardinality
    expected = _distribution_for_frequencies(
        top,
        bottom,
        listed,
        total,
        non_null,
        exhaustive=exhaustive,
    )

    if distribution != expected:
        return [
            Issue(
                col_path,
                "stats.distribution-contradicts-frequencies",
                "error",
                f"distribution={distribution!r} disagrees with frequencies "
                f"(top={top}, bottom={bottom}, listed={listed}, total={total}); "
                f"expected {expected!r}.",
                "§2.2.5",
            ),
        ]

    return []


def _check_redaction_marker(col: dict, col_path: str) -> list[Issue]:
    """A value entry carrying no literal must say why (SPEC 2.2.9)."""

    values = col.get("values")

    if not isinstance(values, list) or col.get("redacted") is not None:
        return []

    if any(isinstance(e, dict) and "value" not in e for e in values):
        return [
            Issue(
                col_path,
                "stats.redacted-without-marker",
                "error",
                "a values entry carries no `value` but the column declares no `redacted` primitive.",
                "§2.2.9",
            ),
        ]

    return []


def _check_unredacted_sensitive(col: dict, col_path: str) -> list[Issue]:
    """SPEC 4.4.2/2.2.9: a detected `sensitivity` publishing a cell value with no marker warns.

    Publication is the same three-field test (`values`/`range`/`percentiles`) the producer's
    marker rule uses (`orchestrator._emitted_extras`). `warning` only: the axis is
    recall-biased (SPEC 4.4.2), so a false positive MUST NOT move the verdict or exit code.
    """

    inferred = col.get("inferred")
    sensitivity = inferred.get("sensitivity") if isinstance(inferred, dict) else None

    if not isinstance(sensitivity, str) or col.get("redacted") is not None:
        return []

    published = [field for field in ("values", "range", "percentiles") if field in col]

    if not published:
        return []

    return [
        Issue(
            col_path,
            "privacy.unredacted-sensitive",
            "warning",
            f"column declares inferred.sensitivity={sensitivity!r} and publishes "
            f"{', '.join(published)} with no redacted primitive covering it.",
            "§4.4",
        ),
    ]


def _check_precision(col: dict, col_path: str) -> list[Issue]:
    """Verify the SPEC 2.2.6 rounding rule: at most six decimal places.

    Temporal bounds are ISO strings with no precision to measure, so only numeric forms are
    read; notation is unreadable from a parsed value, so 2.2.6's rendering half is untested.
    """

    issues: list[Issue] = []

    for field in ("null_rate", "cardinality_ratio", "values_coverage"):
        issues.extend(_precision_issue(col.get(field), col_path, field))

    bounds = col.get("range")

    if isinstance(bounds, dict):
        for edge in ("min", "max"):
            issues.extend(_precision_issue(bounds.get(edge), col_path, f"range.{edge}"))

    percentiles = col.get("percentiles")

    if isinstance(percentiles, dict):
        for key, value in percentiles.items():
            issues.extend(_precision_issue(value, col_path, f"percentiles.{key}"))

    return issues


def _precision_issue(value: object, col_path: str, field: str) -> list[Issue]:
    if not isinstance(value, float):
        return []

    # A non-finite value has no decimal places to count.
    if not math.isfinite(value):
        return []

    if round(value, _PRECISION_DECIMALS) == value:
        return []

    return [
        Issue(
            col_path,
            "stats.excess-precision",
            "error",
            f"{field}={value!r} carries more than {_PRECISION_DECIMALS} decimal places.",
            "§2.2.6",
        ),
    ]


def _check_unrepresentable(col: dict, col_path: str) -> list[Issue]:
    """Verify SPEC 2.2.4's two consistency rules for `unrepresentable`.

    An empty list is a violation, and every name must be a field the column emitted; both
    are relational over the column's other fields, so the JSON Schema cannot state them.
    """

    entries = col.get("unrepresentable")

    if not isinstance(entries, list):
        return []

    if not entries:
        return [
            Issue(
                col_path,
                "stats.unrepresentable-empty",
                "error",
                "unrepresentable is an empty list; omit the key instead.",
                "§2.2.4",
            ),
        ]

    emitted = _emitted_temporal_fields(col)

    return [
        Issue(
            col_path,
            "stats.unrepresentable-names-unemitted-field",
            "error",
            f"unrepresentable names {name!r}, which the column did not emit.",
            "§2.2.4",
        )
        for name in entries
        if isinstance(name, str) and name not in emitted
    ]


def _emitted_temporal_fields(col: dict) -> set[str]:
    """The `min` / `max` / percentile keys this column actually emitted."""

    fields: set[str] = set()
    bounds = col.get("range")

    if isinstance(bounds, dict):
        fields.update(name for name in ("min", "max") if name in bounds)

    percentiles = col.get("percentiles")

    if isinstance(percentiles, dict):
        fields.update(percentiles.keys())

    return fields


def _check_span_days(col: dict, col_path: str) -> list[Issue]:
    """SPEC 2.2.4: `span_days` must equal `day_count(range.min, range.max)`.

    Skipped wherever the bounds cannot be read back: `redacted: mask`/`hash` coarsens
    `span_days` instead (SPEC 2.2.9), `drop` emits neither field, a bound named in
    `unrepresentable` opts out, and an unparseable ISO value is that rule's business.
    """

    if col.get("redacted") in ("mask", "hash"):
        return []

    rng = col.get("range")

    if not isinstance(rng, dict):
        return []

    span_days = rng.get("span_days")

    if not isinstance(span_days, int):
        return []

    unrepresentable = col.get("unrepresentable")

    if isinstance(unrepresentable, list) and {"min", "max"} & set(unrepresentable):
        return []

    earlier = parse_instant(rng.get("min"))
    later = parse_instant(rng.get("max"))

    if earlier is None or later is None:
        return []

    expected = day_count(earlier, later)

    if span_days != expected:
        return [
            Issue(
                col_path,
                "stats.span-days-mismatch",
                "error",
                f"span_days={span_days} but day_count(range.min, range.max)={expected}.",
                "§2.2.4",
            ),
        ]

    return []


def _percentile_value(raw: object, classification: str | None) -> Any:
    """`raw` as a comparable value: a parsed instant for `temporal`, a float otherwise.

    None when it cannot be read back that way - wrong type for the classification,
    non-finite, or unparseable - which covers every `unrepresentable` value, none of which
    round-trip through `datetime.fromisoformat` at Python's proleptic year bounds.
    """

    if classification == "temporal":
        return parse_instant(raw)

    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None

    value = float(raw)

    return None if math.isnan(value) or math.isinf(value) else value


def _percentile_entries(col: dict) -> list[tuple[int, Any, Any]]:
    """(percent, raw value, parsed value) triples from `percentiles`, ordered by percent.

    An entry that cannot be read back is dropped rather than failing the column. The key set
    is configurable (`StatisticsConfig().percentiles`), so whatever subset is present is
    ordered rather than the default five.
    """

    percentiles = col.get("percentiles")

    if not isinstance(percentiles, dict) or not percentiles:
        return []

    classification = col.get("classification")
    entries: list[tuple[int, Any, Any]] = []

    for key, raw in percentiles.items():
        if not isinstance(key, str) or not key.startswith("p"):
            continue

        try:
            percent = int(key[1:])
        except ValueError:
            continue

        parsed = _percentile_value(raw, classification)

        if parsed is not None:
            entries.append((percent, raw, parsed))

    return sorted(entries, key=lambda entry: entry[0])


def _check_percentiles_order(col: dict, col_path: str) -> list[Issue]:
    """SPEC 2.2.4: percentiles ascend with their keys - non-decreasing, not strict.

    A single-valued column publishes the same percentile at every key and is correct.
    """

    entries = _percentile_entries(col)

    return [
        Issue(
            col_path,
            "stats.percentiles-not-ordered",
            "error",
            f"percentiles.p{percent_a:02d}={raw_a!r} exceeds percentiles.p{percent_b:02d}="
            f"{raw_b!r}; percentiles must ascend with their keys.",
            "§2.2.4",
        )
        for (percent_a, raw_a, value_a), (percent_b, raw_b, value_b) in itertools.pairwise(entries)
        if value_a > value_b
    ]


def _check_percentiles_containment(col: dict, col_path: str) -> list[Issue]:
    """SPEC 2.2.4: every percentile lies within `[range.min, range.max]`.

    One statement reads `range` and `percentiles` on every adapter, so a correct producer
    cannot publish a percentile outside its own bounds. Skipped under any `redacted` marker:
    the placeholder carries no literal to compare.
    """

    if col.get("redacted") is not None:
        return []

    rng = col.get("range")

    if not isinstance(rng, dict):
        return []

    classification = col.get("classification")
    lo = _percentile_value(rng.get("min"), classification)
    hi = _percentile_value(rng.get("max"), classification)

    if lo is None or hi is None:
        return []

    numeric = isinstance(lo, (int, float))
    issues: list[Issue] = []

    for percent, raw, value in _percentile_entries(col):
        outside = (
            value < lo - _PERCENTILE_CONTAINMENT_TOLERANCE
            or value > hi + _PERCENTILE_CONTAINMENT_TOLERANCE
            if numeric
            else value < lo or value > hi
        )

        if outside:
            issues.append(
                Issue(
                    col_path,
                    "stats.percentile-outside-range",
                    "error",
                    f"percentiles.p{percent:02d}={raw!r} lies outside range "
                    f"[{rng.get('min')!r}, {rng.get('max')!r}].",
                    "§2.2.4",
                ),
            )

    return issues


def _check_max_age_days_mismatch(col: dict, col_path: str, profiled_at: Any) -> list[Issue]:
    """SPEC 2.2.4: `freshness.max_age_days` must equal `max(0, day_count(range.max, profiled_at))`.

    Skipped wherever the bound cannot be read back the same way: a `max` named in
    `unrepresentable`, an unparseable reading (TIME-only, `infinity`, a BC year), or an absent
    `range`. Any `redacted` marker skips it too - SPEC 2.2.9 coarsens `max_age_days` there.
    """

    if col.get("redacted") is not None:
        return []

    freshness = col.get("freshness")

    if not isinstance(freshness, dict):
        return []

    observed = freshness.get("max_age_days")

    if not isinstance(observed, int):
        return []

    rng = col.get("range")

    if not isinstance(rng, dict):
        return []

    unrepresentable = col.get("unrepresentable")

    if isinstance(unrepresentable, list) and "max" in unrepresentable:
        return []

    earlier = parse_instant(rng.get("max"))
    later = parse_instant(profiled_at)

    if earlier is None or later is None:
        return []

    expected = max(0, day_count(earlier, later))

    if observed != expected:
        return [
            Issue(
                col_path,
                "stats.max-age-days-mismatch",
                "error",
                f"freshness.max_age_days={observed} but "
                f"max(0, day_count(range.max, profiled_at))={expected}.",
                "§2.2.4",
            ),
        ]

    return []


def _check_redacted_day_counts(col: dict, col_path: str) -> list[Issue]:
    """SPEC 2.2.3/2.2.9: a redacted temporal column's derived day counts are coarsened.

    Both counts are arithmetic against a published constant, so full precision reconstructs
    the bound they withhold. Checked under every primitive: `drop` leaves `range` absent but
    still emits `freshness`, so `max_age_days` is reachable when `span_days` is not.
    """

    if col.get("redacted") is None:
        return []

    issues: list[Issue] = []
    freshness = col.get("freshness")

    if isinstance(freshness, dict):
        issues.extend(
            _uncoarsened_issue(freshness.get("max_age_days"), "freshness.max_age_days", col_path),
        )

    rng = col.get("range")

    if isinstance(rng, dict):
        issues.extend(_uncoarsened_issue(rng.get("span_days"), "range.span_days", col_path))

    return issues


def _check_physical_name(col: dict, col_path: str, col_name: str) -> list[Issue]:
    """SPEC 2.2.4: `physical_name` MUST be omitted when it equals the map key."""

    physical_name = col.get("physical_name")

    if not isinstance(physical_name, str) or physical_name != col_name:
        return []

    return [
        Issue(
            col_path,
            "stats.physical-name-matches-key",
            "warning",
            f"physical_name={physical_name!r} equals the map key {col_name!r}; omit the field.",
            "§2.2.4",
        ),
    ]


def _check_sketch(col: dict, col_path: str) -> list[Issue]:
    """SPEC 2.2.14 shape and determinism, not overlap: a well-formed sketch may still be
    wrong, but judging that needs a second column to check it against.
    """

    sketch = col.get("sketch")

    if not isinstance(sketch, dict):
        return []

    issues: list[Issue] = []
    method = sketch.get("method")

    if method != SKETCH_METHOD:
        issues.append(
            Issue(
                col_path,
                "stats.sketch-unknown-method",
                "error",
                f"sketch.method={method!r} is not a recognized SPEC 2.2.14 method.",
                "§2.2.14",
            ),
        )

    hashes = decode_sketch(sketch.get("values")) if isinstance(sketch.get("values"), str) else None

    if hashes is None:
        issues.append(
            Issue(
                col_path,
                "stats.sketch-invalid-encoding",
                "error",
                "sketch.values is not valid base64 of a multiple of 8 bytes "
                "(SPEC 2.2.14's packed big-endian uint64 array).",
                "§2.2.14",
            ),
        )

        return issues

    if len(hashes) > SKETCH_K:
        issues.append(
            Issue(
                col_path,
                "stats.sketch-oversized",
                "error",
                f"sketch carries {len(hashes)} values, more than k={SKETCH_K}.",
                "§2.2.14",
            ),
        )

    if hashes != sorted(hashes):
        issues.append(
            Issue(
                col_path,
                "stats.sketch-not-ascending",
                "error",
                "sketch.values is not ascending; a KMV sketch's k minimums are unordered "
                "or a byte-order mistake in the producer's own hash.",
                "§2.2.14",
            ),
        )

    return issues


def _uncoarsened_issue(value: Any, field: str, col_path: str) -> list[Issue]:
    if not isinstance(value, int) or value % REDACTED_DAY_COUNT_GRANULARITY == 0:
        return []

    return [
        Issue(
            col_path,
            "stats.uncoarsened-redacted-day-count",
            "error",
            f"{field}={value} is not a multiple of {REDACTED_DAY_COUNT_GRANULARITY} on a "
            "column declaring redacted.",
            "§2.2.9",
        ),
    ]


def _distribution_for_categorical(top: int, bot: int, total: int) -> str:
    if top / total >= 0.95:
        return "dominant_value"
    elif top / bot > 2:
        return "imbalanced"
    else:
        return "uniform"


def _distribution_for_frequencies(
    top: int,
    bottom: int,
    listed: int,
    total: int,
    non_null: int,
    *,
    exhaustive: bool,
) -> str:
    """SPEC 2.2.5's priority order recomputed from a top-N summary rather than the ordered
    counts, pinned against the producer's own rule by `test_frequencies_rule_agreement.py`.
    """

    if listed <= 0 or non_null <= 0:
        return "uniform"

    if is_incoherent(total, non_null):
        return _imbalance_or_uniform_from_frequencies(top, bottom, listed, exhaustive=exhaustive)

    if top / non_null >= 0.95:
        return "dominant_value"

    if not exhaustive and total / non_null < 0.30:
        return "long_tail"

    return _imbalance_or_uniform_from_frequencies(top, bottom, listed, exhaustive=exhaustive)


def _imbalance_or_uniform_from_frequencies(
    top: int,
    bottom: int,
    listed: int,
    *,
    exhaustive: bool,
) -> str:
    if exhaustive and listed == 1:
        return "dominant_value"

    return "imbalanced" if bottom > 0 and top / bottom > 2 else "uniform"
