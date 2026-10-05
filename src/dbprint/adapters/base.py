"""Adapter ABC + intermediate dataclass types. See ARCHITECTURE.md 2.

Adapters return typed intermediate records; the engine converts them into on-disk artifacts.
`StatisticsConfig`, `Distribution` and `FreshnessClassification` are re-exported here;
`Freshness` is engine-derived, never adapter output.
"""

from __future__ import annotations

import copy
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from types import ModuleType
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import (
    base_type,
    classify,
    compute_cardinality_ratio,
    compute_null_rate,
    element_type,
    has_day_resolution,
    is_binary_type,
    is_floating_type,
    is_numeric_type,
    is_recognised_type,
    is_spatial_type,
    is_string_like_type,
    is_vector_type,
    map_types,
    record_members,
)
from dbprint.spec.coverage import coverage_share, enumeration_limit
from dbprint.spec.distribution import Distribution, Frequencies
from dbprint.spec.distribution import classify as classify_distribution
from dbprint.spec.distribution import summarize as summarize_frequencies
from dbprint.spec.parts import ELEMENT, KEYS, depth, select_parts
from dbprint.spec.parts import member as member_step
from dbprint.spec.percentiles import coherent_percentiles
from dbprint.spec.rounding import UnrepresentableValue, measured_value, round_statistic
from dbprint.spec.sketch import SketchKind
from dbprint.spec.spatial import Extent, Geometry
from dbprint.spec.temporal_age import FreshnessClassification
from dbprint.spec.temporal_range import is_representable
from dbprint.spec.value_text import scalar_text, value_order_key
from .dialect import Dialect


if TYPE_CHECKING:
    from .identifiers import Identity, IdentityRegistry


__all__ = [
    "MIN_SAMPLE_DRAW",
    "PHASE_A_EXPRESSION_BUDGET",
    "Adapter",
    "AdapterType",
    "BaseStats",
    "ColumnMeta",
    "ColumnParts",
    "ColumnProgress",
    "ColumnReads",
    "ColumnStats",
    "CommentsMeta",
    "Dependency",
    "Detection",
    "Distribution",
    "FkAction",
    "ForeignKeyMeta",
    "Frequencies",
    "Freshness",
    "FreshnessClassification",
    "Grain",
    "GrainDetection",
    "GrainKey",
    "IndexMeta",
    "Inferred",
    "NullPattern",
    "NullPatterns",
    "PartSource",
    "PartStats",
    "PhysicalLayout",
    "PhysicalLayoutKey",
    "Range",
    "SketchKind",
    "SqlAdapter",
    "StatisticsConfig",
    "TableCounts",
    "TableMerging",
    "TableMeta",
    "TableScope",
    "TableType",
    "TemporalShape",
    "UniqueKeyMeta",
    "ValueCount",
    "assemble_column_stats",
    "batched_by_cost",
    "declared_members",
    "descend",
    "empty_base_stats",
    "empty_column_stats",
    "has_measurable_nulls",
    "is_string_like",
    "lookup_operand",
    "lookup_temporal_shape",
    "materialized_name",
    "measure_columns",
    "measures_length",
    "null_flags",
    "null_patterns_from_rows",
    "numeric_block_from_row",
    "phase_a_cost",
    "pre_classify",
    "profile_over",
    "row_count_or_none",
    "seed_from_fqn",
    "temporal_block_unmeasured",
    "temporal_with_top_n",
    "top_n_summary",
    "unrepresentable_fields",
    "value_list_from_rows",
    "whole_temporal_block",
]


AdapterType = Literal[
    "postgres",
    "snowflake",
    "mysql",
    "duckdb",
    "clickhouse",
    "redshift",
    "databricks",
    "bigquery",
]
TableType = Literal["table", "view", "matview"]
FkAction = Literal["NO ACTION", "CASCADE", "SET NULL", "SET DEFAULT", "RESTRICT"]
Detection = Literal["declared", "inferred", "measured"]
GrainDetection = Literal["declared", "measured"]
CardinalityMethod = Literal["exact", "approximate"]
RowCountMethod = Literal["exact", "approximate"]
# What a temporal value is on its own engine - `timestamp` tz-awareness differs per vendor.
TemporalShape = Literal["date", "time", "time_tz", "timestamp", "timestamp_tz", "year"]

# Below this many distinct values a draw is too thin to trust (ARCHITECTURE.md 2).
MIN_SAMPLE_DRAW = 20

# (column_index, column_total, column_name) -> None; 1-based index; None disables.
ColumnProgress = Callable[[int, int, str], None]


def seed_from_fqn(fqn: str, modulus: int) -> int:
    """Sampling seed for one table, derived from its FQN. See ARCHITECTURE.md 2.

    `blake2b`, not the builtin `hash`, whose per-process randomization would break
    reproducibility. `modulus` is the target engine's accepted seed range.
    """

    digest = hashlib.blake2b(fqn.encode("utf-8"), digest_size=8).digest()

    return int.from_bytes(digest, "big") % modulus


def materialized_name(fqn: str) -> str:
    """Unqualified relation name one table's copied sample lives under.

    Derived from the FQN so two tables in one session cannot collide, and short enough for
    every vendor's identifier limit. Qualifying it is the adapter's call.
    """

    digest = hashlib.blake2b(fqn.encode("utf-8"), digest_size=8).hexdigest()

    return f"dbprint_sample_{digest}"


def has_measurable_nulls(counts: TableCounts, base: dict[str, BaseStats]) -> bool:
    """Whether a null census would say anything, settled from Phase A alone.

    No scanned rows, or none carrying a null, means nothing to relate; SPEC 2.2.10 reads an
    absent block as exactly that, so the grouped scan is skipped.
    """

    return bool(counts.rows_scanned) and any(stats.null_count for stats in base.values())


def null_flags(quoted_columns: list[str], *, concat: bool) -> str:
    """One flag per column, joined into the expression a null census groups by.

    `concat` picks the dialect's spelling: the operator form chains to any width, where
    Postgres caps a function call at 100 arguments, but MySQL reads it as OR by default.
    """

    flags = [f"CASE WHEN {column} IS NULL THEN '1' ELSE '0' END" for column in quoted_columns]

    if concat:
        return f"CONCAT({', '.join(flags)})"

    return " || ".join(flags)


def null_patterns_from_rows(
    rows: list[tuple[Any, ...]],
    columns: list[ColumnMeta],
    rows_scanned: int,
    cap: int,
) -> NullPatterns:
    """One grouped scan's `(flags, count)` rows as the SPEC 2.2.10 census.

    `rows` carries one row beyond the cap so truncation is observed rather than predicted.
    Flag positions match `columns`, so the artifact need not state a column order.
    """

    names = [c.name for c in columns]
    entries = [
        NullPattern(
            columns=tuple(
                sorted(name for name, flag in zip(names, flags, strict=True) if flag == "1"),
            ),
            count=int(count),
        )
        for flags, count in rows[:cap]
    ]
    # SPEC 2.2.10 ties break on the name array; the SQL cut orders by the flag string.
    entries.sort(key=lambda pattern: (-pattern.count, pattern.columns))
    listed = sum(pattern.count for pattern in entries)

    # `coverage` is the share of `rows_scanned` the listed entries explain (SPEC 2.2.10).
    # `exhaustive` is the arithmetic fact itself - every scanned row accounted for - never a
    # proxy for "the cap was not hit": an untruncated census can still be incomplete, and
    # `coverage_share`'s clamp would otherwise round a complete sum just under 1.0.
    exhaustive = listed == rows_scanned
    truncated = len(rows) > cap

    # `coverage_method` states whether an untruncated census agreed with `rows_scanned`
    # (SPEC 2.2.10). A truncated one is short by design, a condition the field does not cover.
    coverage_method = None if truncated else ("measured" if exhaustive else "bounded")

    return NullPatterns(
        patterns=tuple(entries),
        coverage=coverage_share(listed, rows_scanned, exhaustive=exhaustive),
        coverage_method=coverage_method,
    )


def row_count_or_none(estimate: float) -> int | None:
    """Normalize a catalog row-count sentinel: negative -> None, else int.

    Zero is a real answer - an analyzed empty table - and stays distinct from unknown.
    """

    return None if estimate < 0 else int(estimate)


def temporal_block_unmeasured(sql_type: str) -> tuple[str, ...]:
    """The REQUIRED fields one failed temporal block costs a column (SPEC 2.2.4).

    `quantized_count` is named only where the type has a day to truncate to: on the others the
    SPEC 2.2.3 matrix never required it, so naming it would claim a measurement nobody was owed.
    """

    lost = ["distribution", "freshness", "frequencies", "percentiles", "range", "values"]

    if has_day_resolution(sql_type):
        lost.append("quantized_count")

    return tuple(sorted(lost))


@dataclass(frozen=True)
class TableMeta:
    """Identifies a table/view/matview and its namespace position.

    `external` marks an object whose rows live in another system (SPEC 2.2.20). `opt_in_only` marks
    a local one profiled only when a `read_rows` rule opts it in; the print never names that mark.
    """

    fqn: str
    type: TableType
    namespace_path: tuple[str, ...]
    external: bool = False
    opt_in_only: bool = False


@dataclass(frozen=True)
class ColumnMeta:
    """Per-column structural metadata sourced from the catalog (no data).

    `name` is the lowercase map key and `physical_name` the catalog's spelling when they differ (SPEC 2.2.1).
    """

    name: str
    sql_type: str
    nullable: bool
    default: str | None
    ordinal: int
    physical_name: str | None = None
    collation: str | None = None
    classify_as: str | None = None

    @property
    def classified_type(self) -> str:
        """The type classification reads - what a user-defined type resolves to, else `sql_type`."""

        return self.classify_as or self.sql_type


@dataclass(frozen=True)
class ForeignKeyMeta:
    """One outgoing FK; arrays for both sides support composite keys.

    Adapters leave `detection` at `declared`; only the engine stamps `inferred`.
    """

    column: tuple[str, ...]
    target_table: str
    target_column: tuple[str, ...]
    on_delete: FkAction
    on_update: FkAction
    constraint_name: str | None
    detection: Detection = "declared"


@dataclass(frozen=True)
class IndexMeta:
    """Secondary index; covers explicit CREATE INDEX, not PK/UNIQUE constraints."""

    name: str
    columns: tuple[str, ...]
    unique: bool
    type: str


@dataclass(frozen=True)
class UniqueKeyMeta:
    """One declared-unique column group and whether it is the primary key."""

    columns: tuple[str, ...]
    primary: bool = False


@dataclass(frozen=True)
class PhysicalLayoutKey:
    """One clustering/partitioning key component, in declaration order.

    `column` is the base column a predicate would filter on, recovered from `expression`
    where possible (`logged_at` from `logged_at::date`); None when there is no single column.
    """

    expression: str
    column: str | None = None


@dataclass(frozen=True)
class PhysicalLayout:
    """A table's declared clustering or partitioning key - never measured; `mechanism` is per
    adapter and `keys` ordered, and absence means "not clustered", never "not checked".
    """

    mechanism: Literal["cluster", "partition", "sort"]
    keys: tuple[PhysicalLayoutKey, ...]


@dataclass(frozen=True)
class TableMerging:
    """A table whose engine combines rows sharing `keys` only when it merges parts (SPEC 2.2.19).

    `rows` says which rows a plain read returns: `stored`, or `merged` where the session reads
    every table as `FINAL`.
    """

    engine: str
    keys: tuple[PhysicalLayoutKey, ...]
    one_row_per_key: bool
    rows: Literal["stored", "merged"]


@dataclass(frozen=True)
class GrainKey:
    """One column combination that identifies a row (SPEC 2.2.12).

    `detection` splits as SPEC 2.3.8 splits a foreign key: `declared` restates a catalog
    constraint, `measured` is a probe over the data at `profiled_at` and guarantees nothing.
    """

    columns: tuple[str, ...]
    detection: GrainDetection


@dataclass(frozen=True)
class Grain:
    """A table's row-identifying key(s): every declared key, plus a bounded measured probe.

    `search_exhausted` is SPEC 2.2.12's tri-state: None when the probe never ran, True when
    every pruned pair was tested, False when the per-table cap cut it short - so "did not
    look" never reads as "looked and found nothing".
    """

    keys: tuple[GrainKey, ...]
    search_exhausted: bool | None = None


@dataclass(frozen=True)
class Dependency:
    """One functional dependency measured over the scanned rows (SPEC 2.2.13).

    `strength` is `cardinality(determinant) / cardinality(determinant, dependent)`: 1.0 when
    every determinant value maps to one dependent value, lower with each extra pairing. A
    measurement, never a constraint.
    """

    determinant: str
    dependent: str
    strength: float


@dataclass(frozen=True)
class TimelineBucket:
    """One bucketed span of the `timeline` anchor column (SPEC 2.2.16)."""

    start: str
    count: int


@dataclass(frozen=True)
class Timeline:
    """The anchor column's activity bucketed at an adaptive unit (SPEC 2.2.16) - `coverage` is the
    listed bucket counts over `rows_scanned`, rounded per SPEC 2.2.6 so a validator cannot disagree.
    """

    column: str
    unit: Literal["day", "week", "month"]
    buckets: tuple[TimelineBucket, ...]
    coverage: float


@dataclass(frozen=True)
class Populated:
    """One column's populated window, dated against the table's `timeline` anchor (SPEC 2.2.4).
    `from_` trails an underscore - `from` is a Python keyword; the serialized key is `from`.
    """

    from_: str
    to: str


@dataclass(frozen=True)
class CommentsMeta:
    """Schema-level comments - distinct from user-authored `description.md`."""

    table: str | None
    columns: dict[str, str]


@dataclass(frozen=True)
class Range:
    """Numeric/temporal min and max; `span_days` is temporal-only."""

    min: Any
    max: Any
    span_days: int | None = None


@dataclass(frozen=True)
class Length:
    """Character length summary (SPEC 2.2.4), on every string-valued classification."""

    min: int
    max: int
    avg: float
    p95: float


@dataclass(frozen=True)
class Freshness:
    """Temporal-column freshness from `Range.max` vs the run's `profiled_at`.

    Engine-computed: no adapter carries `profiled_at`, so no adapter can produce this.
    """

    max_age_days: int
    classification: FreshnessClassification


@dataclass(frozen=True)
class Inferred:
    """Detected-pattern hints - shape, personal-data category, epoch unit and uniqueness flag,
    each an independent axis, none a fallback for another (SPEC 4.5).
    """

    looks_like: str | None = None
    candidate_key: bool | None = None
    candidate_key_exception: str | None = None
    sensitivity: str | None = None
    epoch_unit: str | None = None
    sampled: int | None = None
    matched: int | None = None
    looks_like_candidate: str | None = None
    looks_like_candidate_share: float | None = None


@dataclass(frozen=True)
class ValueCount:
    """One entry in a column's value list: a distinct value and how often it occurs."""

    value: Any
    count: int


@dataclass(frozen=True)
class NullPattern:
    """One exact combination of simultaneously-null columns, and the rows carrying it.

    Every column outside `columns` is populated on those rows, so entries never overlap
    (SPEC 2.2.10); `columns` is sorted, so the tie-break on equal counts is stable.
    """

    columns: tuple[str, ...]
    count: int


@dataclass(frozen=True)
class NullPatterns:
    """A table's null-combination census, capped, with the share of rows it covers.

    `coverage` is computed against `rows_scanned` under the same rounding rule as
    `values_coverage`, so a validator recomputing it cannot disagree with the producer.
    `coverage_method` is None for a truncated census, where the condition does not apply.
    """

    patterns: tuple[NullPattern, ...]
    coverage: float
    coverage_method: str | None = None


@dataclass(frozen=True)
class TableScope:
    """Row-level narrowing in force for one table, per SPEC 2.2.8.

    A predicate or a fraction, never both; both None is a full scan. `materialized` names the
    relation a sampled draw was copied into, so every statement reads one draw - never
    serialized, since SPEC 2.2.8 forbids recording how the sample was drawn. `count_exactly`
    makes a narrowed read count the whole object instead of taking a catalog estimate.
    """

    sample: float | None = None
    filter: str | None = None
    materialized: str | None = None
    count_exactly: bool = False

    def __post_init__(self) -> None:
        if self.sample is not None and self.filter is not None:
            raise ValueError(
                f"scope carries both sample={self.sample!r} and filter={self.filter!r}; "
                f"a table is narrowed by a predicate or by a fraction, never both.",
            )

        if self.materialized is not None and self.sample is None:
            raise ValueError(
                f"scope carries materialized={self.materialized!r} without a sample; only a "
                f"drawn fraction is worth copying - a full scan has nothing to copy, and a "
                f"predicate selects the same rows however often it is evaluated.",
            )

    @property
    def narrows(self) -> bool:
        """True when this scope reads less than the whole table."""

        return self.sample is not None or self.filter is not None


@dataclass(frozen=True)
class TableCounts:
    """One table's counts, and how the total was obtained.

    `row_count_method` is the adapter's own statement, not derived from whether `scope`
    narrowed the read (ARCHITECTURE.md 2, SPEC 2.2.1).
    """

    row_count: int
    rows_scanned: int
    row_count_method: RowCountMethod = "exact"


@dataclass(frozen=True)
class BaseStats:
    """Phase A output for one column. See ARCHITECTURE.md 2 (Intermediate dataclass types).

    `supported` is reported, not re-derived: a declined column is `unsupported` whatever its type name.
    """

    null_count: int
    cardinality: int
    cardinality_method: CardinalityMethod
    supported: bool = True
    zero_count: int | None = None
    negative_count: int | None = None
    empty_count: int | None = None
    quantized_count: int | None = None
    length_min: int | None = None
    length_max: int | None = None
    length_avg: float | None = None
    length_p95: float | None = None


@dataclass(frozen=True)
class ColumnStats:
    """Per-column adapter output; the engine assigns `classification` and `freshness`.

    Always-present fields are the SPEC 2.2.2 universal set, optional ones the SPEC 2.2.3 matrix.
    """

    sql_type: str
    nullable: bool
    null_count: int
    null_rate: float
    cardinality: int | None
    cardinality_ratio: float | None
    cardinality_method: CardinalityMethod | None

    values: tuple[ValueCount, ...] | None = None
    values_coverage: float | None = None
    distribution: Distribution | None = None
    frequencies: Frequencies | None = None
    range: Range | None = None
    percentiles: dict[str, Any] | None = None
    mean: float | None = None
    sum: float | None = None
    zero_count: int | None = None
    negative_count: int | None = None
    empty_count: int | None = None
    quantized_count: int | None = None
    length: Length | None = None
    geometry: Geometry | None = None
    extent: Extent | None = None
    dimension: tuple[int, int] | None = None
    norm: tuple[float, float] | None = None
    types: tuple[tuple[str, int], ...] | None = None
    inferred: Inferred | None = None
    unrepresentable: tuple[str, ...] | None = None
    # SPEC 2.2.4: the REQUIRED fields this run attempted and could not obtain. Names them so
    # their absence is not read as the structural cause SPEC 7.2 would otherwise imply.
    unmeasured: tuple[str, ...] | None = None
    # Why `unmeasured` is set, for the engine's warning; never serialized.
    unmeasured_cause: BaseException | None = field(default=None, compare=False, repr=False)


# An engine compiles the whole statement before reading a row, and phase A builds one aggregate
# expression per statistic per column. A conservative bound, not any one engine's limit.
PHASE_A_EXPRESSION_BUDGET = 600


def batched_by_cost(
    columns: list[ColumnMeta],
    cost: Callable[[ColumnMeta], int],
    budget: int = PHASE_A_EXPRESSION_BUDGET,
) -> Iterator[list[ColumnMeta]]:
    """Groups of `columns` whose summed `cost` stays inside `budget`, never yielding an empty one.

    A column costing more than `budget` still gets a group - one statement per column is the floor.
    """

    group: list[ColumnMeta] = []
    spent = 0

    for column in columns:
        price = cost(column)

        if group and spent + price > budget:
            yield group
            group, spent = [], 0

        group.append(column)
        spent += price

    if group:
        yield group


@dataclass(frozen=True)
class SkippedNamespace:
    """A namespace enumeration found but could not list; the run continues without it."""

    name: str
    cause: str


# SPEC 4.2's 0.9999 candidate-key threshold, with headroom for an estimate's own error.
EXACT_PROBE_RATIO = 0.85


@dataclass(frozen=True)
class PhaseA:
    """Phase A's per-column output, and what it could not measure.

    `unmeasured` maps a column no statement could measure to its null count, read on its own.
    """

    stats: dict[str, BaseStats]
    unmeasured: dict[str, int] = field(default_factory=dict)
    failures: tuple[Exception, ...] = ()
    recount_failure: Exception | None = None


def lookup_temporal_shape(
    shapes: Mapping[str, TemporalShape],
    sql_type: str,
) -> TemporalShape | None:
    """What a value of `sql_type` is under an engine's `shapes` table, or None if not temporal."""

    return shapes.get(base_type(sql_type))


def lookup_operand(operands: Mapping[str, str], expr: str, sql_type: str) -> str:
    """`expr` through the engine's `operands` template for `sql_type`, or unchanged if none."""

    template = operands.get(base_type(sql_type))

    return template.format(expr) if template else expr


def is_string_like(sql_type: str, unsupported: Callable[[str], bool]) -> bool:
    """Whether `sql_type` reads as a string; never a type the engine's `unsupported` declines."""

    return not unsupported(sql_type) and is_string_like_type(sql_type)


def measures_length(sql_type: str, unsupported: Callable[[str], bool]) -> bool:
    """Whether Phase A measures `length` and `empty_count` - characters, or bytes on a binary type."""

    return is_string_like(sql_type, unsupported) or is_binary_type(sql_type)


def phase_a_cost(column: ColumnMeta) -> int:
    """`column`'s weight in a phase A batch; `run_phase_a` costs no declined column."""

    if is_numeric_type(column.classified_type):
        return 5

    if is_string_like_type(column.classified_type) or is_binary_type(column.classified_type):
        return 7

    return 2


def run_phase_a(
    columns: list[ColumnMeta],
    cost: Callable[[ColumnMeta], int],
    statement: Callable[[list[ColumnMeta]], tuple[int, dict[str, BaseStats]]],
    null_counts: Callable[[list[ColumnMeta]], tuple[int, dict[str, int]]],
    recount: Callable[[list[ColumnMeta]], Sequence[Any] | None] | None = None,
    budget: int = PHASE_A_EXPRESSION_BUDGET,
    *,
    declines: Callable[[ColumnMeta], bool] = lambda _: False,
) -> tuple[int, PhaseA]:
    """Run phase A in batches; a failed batch is retried per column, then null-counted alone.

    Every `declines` column is null-counted in one statement and reported unsupported; a spatial
    or vector column is null-counted the same way but stays supported, measured by its own read.
    """

    rows_scanned: int | None = None
    stats: dict[str, BaseStats] = {}
    unmeasured: dict[str, int] = {}
    failures: list[Exception] = []
    declined = [c for c in columns if declines(c) or _described_apart(c)]
    columns = [c for c in columns if c not in declined]

    if declined:
        rows_scanned, nulls = null_counts(declined)
        stats.update(
            (
                column.name,
                BaseStats(
                    null_count=min(nulls[column.name], rows_scanned),
                    cardinality=0,
                    cardinality_method="exact",
                    supported=not declines(column),
                ),
            )
            for column in declined
        )

    for batch in batched_by_cost(columns, cost, budget):
        try:
            counted, measured = statement(batch)
        except Exception as exc:
            # A retry cannot outrun the limit that cancelled the batch; it only multiplies the wait.
            if getattr(exc, "timed_out", False):
                raise

            failures.append(exc)
        else:
            rows_scanned = counted if rows_scanned is None else rows_scanned
            stats.update(measured)
            continue

        for column in batch:
            if len(batch) > 1:
                try:
                    counted, measured = statement([column])
                except Exception as exc:
                    if getattr(exc, "timed_out", False):
                        raise

                    failures.append(exc)
                else:
                    rows_scanned = counted if rows_scanned is None else rows_scanned
                    stats.update(measured)
                    continue

            counted, nulls = null_counts([column])
            rows_scanned = counted if rows_scanned is None else rows_scanned
            unmeasured[column.name] = min(nulls[column.name], rows_scanned)

    rows_scanned = rows_scanned or 0
    recount_failure = None

    if recount is not None:
        recount_failure = _settle_near_unique(columns, stats, rows_scanned, recount)

    return rows_scanned, PhaseA(stats, unmeasured, tuple(failures), recount_failure)


@dataclass(frozen=True)
class PhaseB(Mapping[str, ColumnStats]):
    """Phase B's per-column statistics as a mapping, and the columns no statement could measure.

    `unmeasured` names each column whose statistics failed; `failures` holds their causes in order.
    """

    stats: dict[str, ColumnStats]
    unmeasured: tuple[str, ...] = ()
    failures: tuple[Exception, ...] = ()

    def __getitem__(self, key: str) -> ColumnStats:
        return self.stats[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.stats)

    def __len__(self) -> int:
        return len(self.stats)


def run_phase_b(
    columns: list[ColumnMeta],
    measure: Callable[[ColumnMeta], ColumnStats],
    batch: Callable[[list[ColumnMeta]], dict[str, ColumnStats]] | None = None,
) -> PhaseB:
    """Measure each column on its own; a column whose statement fails is named, never the table.

    `batch` runs first when given, falling back per column; a timeout degrades its column too.
    """

    if batch is not None and columns:
        try:
            return PhaseB(batch(columns))
        except Exception:  # noqa: BLE001, S110 - the fallback below measures each column alone
            pass

    stats: dict[str, ColumnStats] = {}
    unmeasured: list[str] = []
    failures: list[Exception] = []

    for column in columns:
        try:
            stats[column.name] = measure(column)
        except UnrepresentableValue as exc:
            exc.column = column.name
            raise
        except Exception as exc:  # noqa: BLE001 - one column's failure, the table measures on
            unmeasured.append(column.name)
            failures.append(exc)

    return PhaseB(stats, tuple(unmeasured), tuple(failures))


def order_values(entries: Iterable[ValueCount]) -> tuple[ValueCount, ...]:
    """`entries` in SPEC 2.2.4's order: count descending, ties by the text the print publishes."""

    return tuple(sorted(entries, key=lambda v: value_order_key(v.count, scalar_text(v.value))))


ValueList = tuple[tuple[ValueCount, ...], float, bool]
TopN = tuple[Distribution, Frequencies, tuple[ValueCount, ...]]
NumericBlock = tuple[
    Range,
    dict[str, Any],
    Distribution | None,
    Frequencies | None,
    tuple[ValueCount, ...] | None,
    float | None,
    float | None,
]
TemporalBlock = tuple[
    Range,
    dict[str, Any],
    Distribution,
    tuple[str, ...],
    Frequencies,
    tuple[ValueCount, ...],
    int | None,
]
_LENGTH_CLASSIFICATIONS = ("text", "binary", "categorical", "foreign_key_candidate")


@dataclass(frozen=True)
class PartStats:
    """One profiled part of a column (SPEC 2.2.18): its statistics over its own occurrences.

    `stats.sql_type` is the part's own type and `stats.nullable` is ignored; `samples` are up to
    `looks_like_sample_size` distinct non-null values, for the detections a column also gets.
    A `keyed` part is one map key's values, its path spelling the key.
    """

    path: str
    occurrences: int
    stats: ColumnStats
    samples: tuple[Any, ...] = ()
    size: Length | None = None
    keyed: bool = False


@dataclass(frozen=True)
class ColumnParts:
    """What one descent found: the chosen parts, how many paths it found, the column's size."""

    parts: tuple[PartStats, ...]
    found: int
    size: Length | None = None
    cardinality: int | None = None
    empty_count: int | None = None
    norm: tuple[float, float] | None = None
    zero_count: int | None = None


@dataclass(frozen=True)
class PartSource:
    """One node of a descent: a part's path and type, and a source holding one row per occurrence.

    The column itself is the root, with path `""` and the table's own scoped source. A `held`
    node exists only where some instance holds it (a record field, a union member, a document's
    values), so none is found at zero occurrences; a `keyed` node is one map key's values. `unlisted` counts the siblings found but never handed over as nodes, the
    keys past a map's pre-cut.
    """

    path: str
    sql_type: str
    source: str
    operand: str = ""
    held: bool = False
    keyed: bool = False
    unlisted: int = 0


@dataclass(frozen=True)
class InstanceShape:
    """An array's or a map's instances read as wholes: their sizes, the empty ones, the distinct ones.

    `norm`/`zero_count` are set only for a floating-point array of one non-empty length.
    """

    size: Length | None
    empty_count: int
    cardinality: int | None
    norm: tuple[float, float] | None = None
    zero_count: int | None = None


@dataclass(frozen=True)
class ArrayReads:
    """How one engine reads into an array (SPEC 2.2.18), each a function of the array operand.

    `elements` turns a node and its element type into a source of one row per element, named `v`.
    """

    elements: Callable[[PartSource, str], str]
    size: Callable[[str], str]
    distinct: Callable[[str], str] | None
    norm: Callable[[str], str] | None


@dataclass(frozen=True)
class Member:
    """One member a record or union declares: its name as the path spells it, and its type.

    `position` is its 1-based place in the declaration, for an engine that addresses by it.
    """

    name: str
    sql_type: str
    union: bool = False
    position: int = 0


@dataclass(frozen=True)
class RecordReads:
    """How one engine reads into a record or union (SPEC 2.2.18).

    `members` lists what a node declares (none for a node of another kind); `source` turns a
    node and one member into a source of one row per instance holding it, named `v`; `present`
    tests a record value for presence, which a composite row needs spelled its own way.
    """

    members: Callable[[Any, PartSource], list[Member]]
    source: Callable[[PartSource, Member], str]
    present: Callable[[str], str] = lambda operand: f"{operand} IS NOT NULL"


@dataclass(frozen=True)
class MapEntries:
    """A map node read as entries: a source aliased `ent` holding one row per entry, key `k` and
    value `v`, and `size`, the expression counting one instance's entries.
    """

    key_sql_type: str
    value_sql_type: str
    source: str
    size: str


@dataclass(frozen=True)
class MapReads:
    """How one engine reads into a map (SPEC 2.2.18).

    `entries` reads a node as entries, None for a node of another kind; `literal` spells one key,
    of the given key type, for comparison with `ent.k`.
    """

    entries: Callable[[Any, PartSource], MapEntries | None]
    literal: Callable[[Any, str], str]


@dataclass(frozen=True)
class DocumentReads:
    """How one engine reads into a JSON document (SPEC 2.2.18), each value typed by `type_of`.

    `names` are the node types read as documents (lowercased): the column's own and the general,
    object and array names `type_of` returns. `entries` reads a node's object instances as rows
    `(k, v)` and `elements` its array instances as rows `v`, both aliased `ent`, `v` a document;
    `read` turns a document whose type name is given into its scalar value; `size` counts an
    object's keys or an array's elements; `numeric` lists the number names, narrowest first.
    """

    type_of: Callable[[str], str]
    null_name: str
    general: str
    object_name: str
    array_name: str
    key_sql_type: str
    names: frozenset[str]
    numeric: tuple[str, ...]
    entries: Callable[[PartSource], str]
    elements: Callable[[PartSource], str]
    read: Callable[[str, str], str]
    size: Callable[[str], str]
    literal: Callable[[Any, str], str]

    def holds(self, node: PartSource) -> bool:
        """Whether `node`'s values are documents this engine descends into.

        The array name counts only bare, as a value's type: a declared array type is not one.
        """

        base = base_type(node.sql_type)

        return base in self.names and (
            base != self.array_name.lower() or node.sql_type.strip().lower() == base
        )

    def value_type(self, found: Iterable[str]) -> str:
        """A part's type from the names its non-null values hold: the one, the widest number, or
        the general document type.
        """

        names = set(found)

        if len(names) == 1:
            return names.pop()

        if names and names <= set(self.numeric):
            return max(names, key=self.numeric.index)

        return self.general


def key_literal(key: Any, key_sql_type: str, *, backslash_escapes: bool = False) -> str:
    """`key` as a SQL literal of `key_sql_type`: an integer bare, a string quoted, anything else cast.

    `backslash_escapes` names a dialect whose string literals treat `\\` as an escape.
    """

    if isinstance(key, int) and not isinstance(key, bool):
        return str(key)

    text = scalar_text(key)
    quoted = (
        "'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"
        if backslash_escapes
        else "'" + text.replace("'", "''").replace("\n", "' || CHR(10) || '") + "'"
    )

    return quoted if isinstance(key, str) else f"CAST({quoted} AS {key_sql_type})"


def declared_maps(
    entries: Callable[[PartSource, str, str], tuple[list[str], str]],
    size: Callable[[str], str],
    *,
    backslash_escapes: bool = False,
) -> MapReads:
    """Map reads for an engine whose map type declares its key and value types.

    `entries` gives the select list (`k`, `v`) and source reading a node, of the given key and
    value types, as one row per entry; `size` counts one instance's entries.
    """

    def read(_cursor: Any, node: PartSource) -> MapEntries | None:
        from .sql_layout import derived, select_from

        declared = map_types(node.sql_type)

        if declared is None:
            return None

        items, source = entries(node, *declared)

        return MapEntries(*declared, derived(select_from(items, source), "ent"), size(node.operand))

    return MapReads(
        entries=read,
        literal=partial(key_literal, backslash_escapes=backslash_escapes),
    )


def declared_members(sql_type: str) -> list[Member]:
    """The members `sql_type` declares in its own spelling, as `Member`s; none for another kind."""

    declared = record_members(sql_type)

    if declared is None:
        return []

    kind, members = declared

    return [
        Member(name=name, sql_type=member_type, union=kind == "union", position=position)
        for position, (name, member_type) in enumerate(members, start=1)
    ]


def dotted_records(dialect: Dialect) -> RecordReads:
    """Record reads for an engine naming a member as `record.member`, from the declared type."""

    def member_source(node: PartSource, member: Member) -> str:
        from .identifiers import SOURCE_ALIAS, quote
        from .sql_layout import derived, select_from

        statement = select_from([f"{node.operand}.{quote(member.name, dialect)} AS v"], node.source)

        return derived(f"{statement}\nWHERE\n  {node.operand} IS NOT NULL", SOURCE_ALIAS)

    return RecordReads(
        members=lambda _cursor, node: declared_members(node.sql_type),
        source=member_source,
    )


def profile_over(
    column: ColumnMeta,
    phase_a: Callable[[list[ColumnMeta]], tuple[int, PhaseA]],
    phase_b: Callable[[list[ColumnMeta], TableCounts, dict[str, BaseStats]], PhaseB],
) -> ColumnStats:
    """One column's statistics over a source of its own, by the reads a table's columns get.

    A part is measured this way, so its block is the block its classification gives a column.
    """

    rows, base = phase_a([column])

    if column.name in base.unmeasured:
        raise base.failures[0] if base.failures else RuntimeError("phase A did not measure")

    counts = TableCounts(row_count=rows, rows_scanned=rows)
    measured = phase_b([column], counts, base.stats)

    if column.name not in measured.stats:
        raise measured.failures[0] if measured.failures else RuntimeError("phase B did not measure")

    return measured.stats[column.name]


def descend(
    root: PartSource,
    config: StatisticsConfig,
    *,
    children: Callable[[PartSource], list[PartSource]],
    occurrences: Callable[[PartSource], int],
    profile: Callable[[PartSource, int], PartStats],
    size: Length | None = None,
) -> ColumnParts:
    """Walk a column's parts to `max_part_depth`, choose them with `select_parts`, profile each.

    `children` is the kind-specific step (an array's elements, a record's members, ...), so an
    array of records composes without a step knowing about the other.
    """

    found: dict[str, PartSource] = {}
    counts: dict[str, int] = {}
    unlisted = 0
    frontier = [root]

    while frontier:
        reached = [child for node in frontier for child in children(node)]
        counted = [
            (child, occurrences(child))
            for child in reached
            if depth(child.path) <= config.max_part_depth
        ]
        held = [(child, count) for child, count in counted if count or not child.held]

        for child, count in held:
            found[child.path] = child
            counts[child.path] = count
            unlisted += child.unlisted

        frontier = [child for child, _ in held if depth(child.path) < config.max_part_depth]

    chosen = select_parts(counts, config.max_parts, config.max_part_depth)

    return ColumnParts(
        parts=tuple(profile(found[path], counts[path]) for path in chosen),
        found=len(found) + unlisted,
        size=size,
    )


class ExtentNotMeasured(Exception):
    """A spatial read that obtained `geometry` but could not bound the values (SPEC 2.2.4)."""

    def __init__(self, geometry: Geometry, reason: str) -> None:
        super().__init__(reason)
        self.geometry = geometry


SpatialRead = Callable[[ColumnMeta], tuple[Geometry, Extent | None]]


def spatial_column_stats(
    col: ColumnMeta,
    null_count: int,
    null_rate: float,
    read: SpatialRead | None,
) -> ColumnStats:
    """A `spatial` column's statistics: the null profile and `read`'s `geometry`/`extent`.

    A failed read names both fields `unmeasured`, one that could not bound the values `extent`.
    """

    stats = ColumnStats(
        sql_type=col.sql_type,
        nullable=col.nullable,
        null_count=null_count,
        null_rate=null_rate,
        cardinality=None,
        cardinality_ratio=None,
        cardinality_method=None,
    )

    try:
        if read is None:
            raise NotImplementedError("this engine has no spatial read")

        geometry, extent = read(col)
    except ExtentNotMeasured as exc:
        return replace(stats, geometry=exc.geometry, unmeasured=("extent",), unmeasured_cause=exc)
    except Exception as exc:  # noqa: BLE001 - the spatial read degrades as a whole
        return replace(stats, unmeasured=("extent", "geometry"), unmeasured_cause=exc)

    return replace(stats, geometry=geometry, extent=extent)


@dataclass(frozen=True)
class VectorReading:
    """A `vector` column's SPEC 2.2.4 aggregates; `unmeasured` names what the engine cannot express."""

    dimension: tuple[int, int] | None
    norm: tuple[float, float] | None
    zero_count: int | None
    unmeasured: tuple[str, ...] = ()


VectorRead = Callable[[ColumnMeta], VectorReading]


def vector_column_stats(
    col: ColumnMeta,
    null_count: int,
    null_rate: float,
    read: VectorRead | None,
) -> ColumnStats:
    """A `vector` column's statistics: the null profile and `read`'s dimension, norm, zero count."""

    stats = ColumnStats(
        sql_type=col.sql_type,
        nullable=col.nullable,
        null_count=null_count,
        null_rate=null_rate,
        cardinality=None,
        cardinality_ratio=None,
        cardinality_method=None,
    )

    try:
        if read is None:
            raise NotImplementedError("this engine has no vector read")

        reading = read(col)
    except Exception as exc:  # noqa: BLE001 - the vector read degrades as a whole
        return replace(stats, unmeasured=("dimension", "norm", "zero_count"), unmeasured_cause=exc)

    return replace(
        stats,
        dimension=reading.dimension,
        norm=reading.norm,
        zero_count=reading.zero_count,
        unmeasured=reading.unmeasured or None,
    )


@dataclass(frozen=True)
class ColumnReads:
    """One engine's Phase B statements; each read takes `(column, non_null, config)`.

    `temporal` returns the finished column; `length_p95`, where set, drops `length` on None.
    """

    value_list: Callable[[ColumnMeta, int, StatisticsConfig], ValueList]
    numeric_block: Callable[..., NumericBlock]
    temporal: Callable[[ColumnStats, ColumnMeta, int, StatisticsConfig], ColumnStats]
    length_p95: Callable[[ColumnMeta], float | None] | None = None
    spatial: SpatialRead | None = None
    vector: VectorRead | None = None


def measure_columns(
    columns: list[ColumnMeta],
    config: StatisticsConfig,
    counts: TableCounts,
    base: dict[str, BaseStats],
    fk_source_columns: frozenset[str],
    reads: ColumnReads,
    *,
    suppress_values: frozenset[str] = frozenset(),
    on_column: ColumnProgress | None = None,
    scope: TableScope | None = None,
) -> PhaseB:
    """Phase B through `reads`: each column pre-classified, then measured on its own."""

    if not columns:
        return PhaseB({})

    if counts.rows_scanned == 0:
        if scope is not None and scope.narrows:
            # A narrowed read drew nothing; an exact, exhaustive shape for columns
            # nobody read would overclaim (SPEC 2.2.7).
            return PhaseB({})

        # A spatial or vector read of no rows is still its answer: empty kinds, no bounds.
        empty = PhaseB(
            {
                c.name: empty_column_stats(c, supported=base[c.name].supported)
                for c in columns
                if not _described_apart(c)
            },
        )
        apart = [c for c in columns if _described_apart(c)]

        if not apart:
            return empty

        measured = run_phase_b(
            apart,
            lambda col: (
                spatial_column_stats(col, base[col.name].null_count, 0.0, reads.spatial)
                if is_spatial_type(col.classified_type)
                else vector_column_stats(col, base[col.name].null_count, 0.0, reads.vector)
            ),
        )

        return PhaseB({**empty.stats, **measured.stats}, measured.unmeasured, measured.failures)

    total = len(columns)
    position = {col.name: index for index, col in enumerate(columns, start=1)}

    def measure(col: ColumnMeta) -> ColumnStats:
        if on_column is not None:
            on_column(position[col.name], total, col.name)

        pre = pre_classify(
            col,
            base[col.name].cardinality,
            config,
            col.name in fk_source_columns,
            supported=base[col.name].supported,
        )

        return assemble_column_stats(
            col,
            base[col.name],
            counts.rows_scanned,
            pre,
            config,
            reads,
            suppressed=col.name in suppress_values,
        )

    return run_phase_b(columns, measure)


def assemble_column_stats(
    col: ColumnMeta,
    base: BaseStats,
    rows_scanned: int,
    pre: str,
    config: StatisticsConfig,
    reads: ColumnReads,
    *,
    suppressed: bool = False,
) -> ColumnStats:
    """One column's ColumnStats from Phase A's `base` plus the reads `pre` calls for."""

    null_count = base.null_count
    null_rate = compute_null_rate(null_count, rows_scanned)

    if pre == "spatial":
        return spatial_column_stats(col, null_count, null_rate, reads.spatial)

    if pre == "vector":
        return vector_column_stats(col, null_count, null_rate, reads.vector)

    if pre == "unsupported":
        return ColumnStats(
            sql_type=col.sql_type,
            nullable=col.nullable,
            null_count=null_count,
            null_rate=null_rate,
            cardinality=None,
            cardinality_ratio=None,
            cardinality_method=None,
        )

    cardinality = int(base.cardinality)
    non_null = rows_scanned - null_count
    stats = ColumnStats(
        sql_type=col.sql_type,
        nullable=col.nullable,
        null_count=null_count,
        null_rate=null_rate,
        cardinality=cardinality,
        cardinality_ratio=compute_cardinality_ratio(cardinality, rows_scanned),
        cardinality_method=base.cardinality_method,
        # Phase A gates these on raw sql_type, not on `pre` - a numeric type that classifies
        # categorical would otherwise carry a field its own classification forbids.
        zero_count=base.zero_count if pre == "numeric" else None,
        negative_count=base.negative_count if pre == "numeric" else None,
        empty_count=base.empty_count if pre in ("text", "binary") else None,
        quantized_count=base.quantized_count if pre == "numeric" else None,
        length=_length(col, base, reads) if pre in _LENGTH_CLASSIFICATIONS else None,
    )

    if pre in ("json", "binary"):
        return stats
    elif pre == "boolean":
        values, coverage, _ = reads.value_list(col, non_null, config)

        return replace(stats, values=values, values_coverage=coverage)
    elif pre == "numeric":
        # A suppressed numeric column is an array's pooled float elements: no list is published.
        rng, percentiles, distribution, frequencies, values, mean, total = reads.numeric_block(
            col,
            non_null,
            config,
            values=not suppressed,
        )

        return replace(
            stats,
            range=rng,
            percentiles=percentiles,
            distribution=distribution,
            frequencies=frequencies,
            values=values,
            mean=mean,
            sum=total,
        )
    elif pre == "temporal":
        return reads.temporal(stats, col, non_null, config)
    elif suppressed and pre == "text":
        # The only suppressible classification; `distribution` goes with the list.
        return stats

    values, coverage, exhaustive = reads.value_list(col, non_null, config)
    distribution = classify_distribution([v.count for v in values], non_null, exhaustive=exhaustive)

    return replace(stats, values=values, values_coverage=coverage, distribution=distribution)


def whole_temporal_block(
    fetch: Callable[[ColumnMeta, int, StatisticsConfig], TemporalBlock],
    stats: ColumnStats,
    col: ColumnMeta,
    non_null: int,
    config: StatisticsConfig,
) -> ColumnStats:
    """The temporal read of an engine fetching the block in one piece: a failure costs all of it."""

    try:
        rng, percentiles, distribution, unrepresentable, frequencies, values, quantized = fetch(
            col,
            non_null,
            config,
        )
    except UnrepresentableValue:
        raise
    except Exception as exc:  # noqa: BLE001 - the temporal block degrades as a whole
        # Its fields are REQUIRED (SPEC 2.2.3), so the column names the read's cost, not a cause.
        return replace(
            stats,
            unmeasured=temporal_block_unmeasured(col.classified_type),
            unmeasured_cause=exc,
        )

    return replace(
        stats,
        range=rng,
        percentiles=percentiles,
        distribution=distribution,
        frequencies=frequencies,
        unrepresentable=unrepresentable or None,
        values=values,
        quantized_count=quantized,
    )


def temporal_with_top_n(
    stats: ColumnStats,
    rng: Range,
    percentiles: dict[str, Any],
    quantized_count: int | None,
    top_n: Callable[[], TopN],
) -> ColumnStats:
    """A temporal column whose bounds were read apart from its top-N; only the top-N can degrade."""

    unrepresentable = unrepresentable_fields(rng, percentiles)

    try:
        distribution, frequencies, values = top_n()
    except UnrepresentableValue:
        raise
    except Exception as exc:  # noqa: BLE001 - only top-N is guarded; bounds survive
        # `distribution`/`frequencies` are REQUIRED (SPEC 2.2.3); empty counts would read `uniform`.
        return replace(
            stats,
            range=rng,
            percentiles=percentiles,
            unrepresentable=unrepresentable or None,
            quantized_count=quantized_count,
            unmeasured=("distribution", "frequencies", "values"),
            unmeasured_cause=exc,
        )

    return replace(
        stats,
        range=rng,
        percentiles=percentiles,
        distribution=distribution,
        frequencies=frequencies,
        unrepresentable=unrepresentable or None,
        values=values,
        quantized_count=quantized_count,
    )


def pre_classify(
    col: ColumnMeta,
    cardinality: int,
    config: StatisticsConfig,
    has_declared_fk: bool,
    *,
    supported: bool,
) -> str:
    """Declined when Phase A reported the column unsupported, else the shared SPEC 3.2 decision.

    `supported` is Phase A's verdict: a declined type may match no table (a Postgres composite).
    """

    if not supported:
        return "unsupported"

    return classify(
        col.classified_type,
        cardinality,
        has_declared_fk,
        config.enumeration_threshold,
    )


def empty_column_stats(
    col: ColumnMeta,
    *,
    supported: bool,
    method: CardinalityMethod = "exact",
) -> ColumnStats:
    """SPEC 2.2.7 edge case: a table read in full and found empty -> minimal column stats.

    A narrowed read that drew nothing is a different condition and never reaches here.
    """

    if not supported:
        return ColumnStats(
            sql_type=col.sql_type,
            nullable=col.nullable,
            null_count=0,
            null_rate=0.0,
            cardinality=None,
            cardinality_ratio=None,
            cardinality_method=None,
        )

    return ColumnStats(
        sql_type=col.sql_type,
        nullable=col.nullable,
        null_count=0,
        null_rate=0.0,
        cardinality=0,
        cardinality_ratio=0.0,
        cardinality_method=method,
    )


def empty_base_stats(*, supported: bool, method: CardinalityMethod = "exact") -> BaseStats:
    """Phase A's answer for a column whose batched query yielded no row."""

    return BaseStats(null_count=0, cardinality=0, cardinality_method=method, supported=supported)


def value_list_from_rows(
    rows: Sequence[Sequence[Any]],
    non_null: int,
    config: StatisticsConfig,
) -> ValueList:
    """A value-list statement's `(value, count)` rows as the list, its coverage, and exhaustiveness.

    `rows` holds one row past the limit, so truncation is observed, not predicted (SPEC 2.2.4).
    """

    limit = enumeration_limit(config.enumeration_threshold, config.top_n_values)
    exhaustive = len(rows) <= limit
    kept = rows if exhaustive else rows[: config.top_n_values]
    values = order_values(
        ValueCount(value=measured_value(value, f"values[{i}]"), count=int(cnt))
        for i, (value, cnt) in enumerate(kept)
    )
    total = sum(v.count for v in values)

    return values, coverage_share(total, non_null, exhaustive=exhaustive), exhaustive


def top_n_summary(
    rows: Sequence[Sequence[Any]],
    non_null: int,
    config: StatisticsConfig,
    value_transform: Callable[[Any], Any],
) -> TopN:
    """Distribution, frequencies, and the same top-N rows `values` publishes (SPEC 2.2.3)."""

    limit = enumeration_limit(config.enumeration_threshold, config.top_n_values)
    exhaustive = len(rows) <= limit
    kept = rows if exhaustive else rows[: config.top_n_values]
    values = order_values(
        ValueCount(value=value_transform(value), count=int(cnt)) for value, cnt in kept
    )
    kept_counts = [v.count for v in values]

    return (
        classify_distribution(kept_counts, non_null, exhaustive=exhaustive),
        summarize_frequencies(kept_counts),
        values,
    )


def numeric_block_from_row(
    row: Sequence[Any] | None,
    percentile_values: Sequence[Any],
    config: StatisticsConfig,
    top_n: Callable[[], TopN] | None,
    exact: Callable[[Any], Any] = lambda value: value,
) -> NumericBlock:
    """A numeric column's block from its `(min, max, avg, sum)` row and percentiles in key order.

    `exact` reads a bound or sum the driver hands back as something other than a number; with
    no `top_n` the value list is not read and its three fields are None.
    """

    if row is None:
        return Range(min=None, max=None), {}, "uniform", summarize_frequencies([]), (), None, None

    rng = Range(
        min=round_statistic(exact(row[0]), exact_int=True),
        max=round_statistic(exact(row[1]), exact_int=True),
    )
    percentiles = coherent_percentiles(
        {
            f"p{p:02d}": round_statistic(v)
            for p, v in zip(config.percentiles, percentile_values, strict=True)
        }
        if len(percentile_values)
        else {},
        rng.min,
        rng.max,
    )
    distribution, frequencies, values = top_n() if top_n is not None else (None, None, None)

    return (
        rng,
        percentiles,
        distribution,
        frequencies,
        values,
        round_statistic(row[2]),
        round_statistic(exact(row[3]), exact_int=True),
    )


def unrepresentable_fields(rng: Range, percentiles: dict[str, Any]) -> tuple[str, ...]:
    """Field names per SPEC 2.2.4 whose rendered text names a year outside 0001-9999."""

    names = []

    if rng.min is not None and not is_representable(rng.min):
        names.append("min")

    if rng.max is not None and not is_representable(rng.max):
        names.append("max")

    for key in sorted(percentiles):
        value = percentiles[key]

        if value is not None and not is_representable(value):
            names.append(key)

    return tuple(names)


class Adapter(ABC):
    """Single integration surface for a database. See ARCHITECTURE.md 2.

    One instance is one session, used by one thread at a time; `new_session` makes more.
    """

    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ()
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = ()
    # Credential keys naming a local file: resolved against the project root, refused if absent.
    PATH_KEYS: ClassVar[tuple[str, ...]] = ()

    # Whether an unmaterialized `sample` scope stays coherent across statements - a seeded per-row
    # predicate (Postgres/duckdb BERNOULLI) redraws identically, an unseeded construct does not.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = True

    # Whether `materialize_scope`'s copy dies with the session on its own, so a failed
    # `release_scope` still leaves nothing behind. False names what cleans it up instead.
    MATERIALIZED_SCOPE_SESSION_SCOPED: ClassVar[bool] = True
    # The vendor spellings this adapter knowingly declines or profiles as text by representability.
    KNOWN_TYPES: ClassVar[tuple[str, ...]] = ()

    def recognises_type(self, sql_type: str) -> bool:
        """Whether `sql_type` is named by a shared table or this adapter's own declared spellings."""

        return is_recognised_type(sql_type) or base_type(sql_type) in self.KNOWN_TYPES

    @abstractmethod
    def connect(self) -> None:
        """Open the underlying connection and verify any external dependencies."""

    @abstractmethod
    def close(self) -> None:
        """Release the connection; idempotent."""

    @abstractmethod
    def new_session(self) -> Self:
        """Another unconnected instance on the same target, sharing this one's identifier maps.

        Called after `list_tables`, which rebinds the maps a session spawned earlier would miss.
        """

    @abstractmethod
    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        """Enumerate tables/views/matviews in scope per fnmatch selectors.

        Lowercased FQNs match against include/exclude (SPEC 6, ARCHITECTURE.md 6); an empty
        `include` matches nothing.
        """

    @abstractmethod
    def extract_ddl(self, fqn: str) -> str:
        """Return native-dialect DDL for the object, post-normalization (SPEC 2.1)."""

    @abstractmethod
    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        """Return per-column structural metadata in ordinal order."""

    @abstractmethod
    def default_collation(self) -> str:
        """Return the connection's effective default string-comparison collation (SPEC 2.2.2).

        Catalog metadata only - no scan, once per connection. `ColumnMeta.collation` is None
        for a column comparing under this default, set only where the two disagree.
        """

    @abstractmethod
    def introspect_relationships(self, fqn: str) -> list[ForeignKeyMeta]:
        """Return declared outgoing FKs; composite FKs as single entries."""

    @abstractmethod
    def introspect_indexes(self, fqn: str) -> list[IndexMeta]:
        """Return secondary indexes only; disjoint from `introspect_unique_keys` per SPEC 2.6.7."""

    @abstractmethod
    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        """Return declared-unique column groups, one per constraint, in declaration order.

        Catalog metadata only - no scan; at most one group is primary. Per SPEC 2.6.7:
        includes a bare unique index backing no constraint, excludes a partial unique index
        and whatever `introspect_indexes` reports.
        """

    @abstractmethod
    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        """Return the table's declared clustering/partitioning key, or None.

        Catalog metadata only - no scan, never a measurement of how well clustered the table
        is. None means no such key is declared, and every adapter MUST answer.
        """

    def introspect_merging(self, fqn: str) -> TableMerging | None:
        """Return the table's merging engine and sorting key (SPEC 2.2.19), or None.

        Catalog metadata only; None means the engine combines no rows, and an adapter with no
        such engine keeps this default.
        """

        del fqn

        return None

    @abstractmethod
    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]] | None:
        """Each view's direct dependencies within the namespaces the last `list_tables` selected.

        `None` or a missing key: not asked; a failed namespace is in `unread_dependency_namespaces`.
        """

    @abstractmethod
    def extract_comments(self, fqn: str) -> CommentsMeta:
        """Return table comment and per-column comments from catalog metadata."""

    @abstractmethod
    def estimate_row_count(self, fqn: str) -> int | None:
        """Catalog row-count estimate, no scan; None is no estimate, 0 an analyzed empty table."""

    @abstractmethod
    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, PhaseA]:
        """Phase A: counts plus per-column null_count and cardinality. See ARCHITECTURE.md 2."""

    @abstractmethod
    def compute_column_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        fk_source_columns: frozenset[str],
        *,
        suppress_values: frozenset[str] = frozenset(),
        on_column: ColumnProgress | None = None,
        scope: TableScope | None = None,
    ) -> PhaseB:
        """Phase B: classification-specific statistics, keyed by column name. See ARCHITECTURE.md 2.

        `counts`/`base` are Phase A's output, passed back rather than recomputed; a column in
        `suppress_values` emits no `values`, `values_coverage` or `distribution`. `on_column`
        fires per column, 1-based, unguarded. Adapters MUST NOT stamp `classification`.
        """

    def compute_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        fk_source_columns: frozenset[str],
        on_column: ColumnProgress | None = None,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, PhaseB]:
        """Both phases in order, for a caller with nothing to decide between them."""

        counts, phase_a = self.compute_base_statistics(fqn, columns, config, scope)
        stats = self.compute_column_statistics(
            fqn,
            [c for c in columns if c.name in phase_a.stats],
            config,
            counts,
            phase_a.stats,
            fk_source_columns,
            on_column=on_column,
            scope=scope,
        )

        return counts, stats

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        """Copy a sampled draw into a session-lifetime relation and name it on the scope.

        The default declines: an adapter that cannot write returns `scope` untouched and keeps
        re-evaluating its sampling construct per statement. Raising reports a refused write,
        and the caller falls back to the unmaterialized scope rather than failing the table.
        """

        del fqn

        return scope

    def skipped_namespaces(self) -> tuple[SkippedNamespace, ...]:
        """Namespaces the last `list_tables` found but could not list; empty unless it enumerated."""

        return ()

    def unread_dependency_namespaces(self) -> tuple[SkippedNamespace, ...]:
        """Namespaces the last `introspect_view_dependencies` could not read; empty by default."""

        return ()

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        """Drop whatever `materialize_scope` created; a no-op on a scope it declined.

        Takes `fqn` because an adapter addressing objects by a fully-qualified physical
        identifier needs the table to resolve where its copy was put.
        """

        del fqn, scope

    @abstractmethod
    def compute_null_patterns(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        scope: TableScope | None = None,
    ) -> NullPatterns | None:
        """Which columns are null together, as one grouped scan. See SPEC 2.2.10.

        Returns None with nothing to relate - no rows scanned, or no null in `base`, which
        settles it without touching the database. `config.top_n_null_patterns` caps the
        list; what the cap leaves out shows as coverage below 1.
        """

    @abstractmethod
    def probe_grain(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        candidates: tuple[tuple[str, str], ...],
        scope: TableScope | None = None,
    ) -> tuple[tuple[str, str], ...]:
        """Return the candidate pairs whose distinct count equals the row count (SPEC 2.2.12).

        The caller prunes and caps `candidates`; without batching, one query counts each pair.
        """

    @abstractmethod
    def probe_timeline(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        column: str,
        unit: Literal["day", "week", "month"],
        scope: TableScope | None = None,
    ) -> tuple[tuple[str, int], ...]:
        """Bucket `column`'s non-null values at `unit` grain, one grouped statement (SPEC 2.2.16)
        - ascending (bucket_start, count) pairs; an empty bucket is absent, never a zero entry.
        """

    @abstractmethod
    def compute_populated_windows(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        anchor_column: str,
        subject_columns: tuple[str, ...],
        scope: TableScope | None = None,
    ) -> dict[str, tuple[str, str]]:
        """Each subject column's [from, to] window over the anchor, one statement (SPEC 2.2.4) -
        a subject with no non-null row is absent, and both instants use the anchor's domain rule.
        """

    @abstractmethod
    def probe_dependencies(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        base: dict[str, BaseStats],
        candidates: tuple[tuple[str, str], ...],
        scope: TableScope | None = None,
    ) -> dict[tuple[str, str], float]:
        """Measure `cardinality(determinant, dependent)` per candidate pair. See SPEC 2.2.13.

        `candidates` is already pruned and capped, each entry ordered `(determinant,
        dependent)`; `base` carries `cardinality(determinant)`, so only the joint count needs a
        statement. Returns a strength per candidate with a nonzero joint count, never a verdict.
        """

    def profile_parts(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        scope: TableScope | None = None,
    ) -> dict[str, ColumnParts]:
        """The parts of each of `columns` this adapter descends into (SPEC 2.2.18), by name.

        A column absent from the result was not descended; the engine calls this only while
        `max_parts` is positive, and an adapter that descends into nothing keeps this default.
        """

        del fqn, columns, config, counts, scope

        return {}

    def document_types(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        scope: TableScope | None = None,
    ) -> dict[str, tuple[tuple[str, int], ...]]:
        """How many values of each document column hold each engine type name (SPEC 2.2.4).

        A column absent from the result has no `types`; an engine naming no per-value type keeps
        this default.
        """

        del fqn, columns, scope

        return {}

    @abstractmethod
    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
        sql_type: str | None = None,
    ) -> list[Any]:
        """Return up to n distinct non-null values drawn uniformly (SPEC 4.1.2), for looks_like.

        `column` is the lowercased artifact key; given `sql_type`, strings come as `render_text`.
        """

    @abstractmethod
    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: SketchKind,
        k: int,
    ) -> tuple[int, ...]:
        """A KMV sketch of the join key: the k smallest `low64_md5(canonical(v))`, ascending.

        `kind` picks the canonical encoding family; `sql_type` supplies the temporal rendering
        `kind` alone does not distinguish (SPEC 2.2.14). Computed in-database. Fewer than `k`
        entries means an exact rather than estimated sketch. Never receives a `scope`.
        """

    @abstractmethod
    def compute_normalized_cardinality(
        self,
        fqn: str,
        column: str,
        scope: TableScope | None = None,
    ) -> int:
        """The distinct count of `column` once trimmed and case-folded (SPEC 2.2.4) - computed
        in-database over the set `scope` narrows `cardinality` to (SPEC 2.2.8). A materialized
        `scope` is the same rows; without one this is a later read of a table that may have moved.
        """

    @abstractmethod
    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Execute user-authored read-only SQL; return row tuples, column order preserved.

        Read-only session per ASSERTIONS.md 3.4; errors propagate for the SQL assertion
        evaluator to turn into an Issue.
        """


class SqlAdapter(Adapter):
    """An adapter reading one SQL engine through its own helper modules, which a subclass names.

    `_handle` is what a table's statements run on; `_read_identity` what a column-keyed read names.
    """

    DIALECT: ClassVar[Dialect]
    _driver: ClassVar[ModuleType]
    _introspect: ClassVar[ModuleType]
    _ddl: ClassVar[ModuleType]
    _stats: ClassVar[ModuleType]
    _looks_like: ClassVar[ModuleType]
    _sketch: ClassVar[ModuleType]
    _normalization: ClassVar[ModuleType]
    _not_connected: ClassVar[type[Exception]]

    _connection: Any
    _identities: IdentityRegistry

    def connect(self) -> None:
        self._connection.open()

    def close(self) -> None:
        self._connection.close()

    def new_session(self) -> Self:
        session = copy.copy(self)
        session._connection = self._connection.sibling()

        return session

    def extract_ddl(self, fqn: str) -> str:
        return self._ddl.extract_ddl(self._handle(fqn), self._identity(fqn))

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        columns = self._introspect.columns(self._handle(fqn), self._identity(fqn))
        self._identities.attach(fqn, columns)

        return columns

    def default_collation(self) -> str:
        return self._introspect.default_collation(self._cursor)

    def introspect_relationships(self, fqn: str) -> list[ForeignKeyMeta]:
        return self._introspect.relationships(self._handle(fqn), self._identity(fqn))

    def introspect_indexes(self, fqn: str) -> list[IndexMeta]:
        return self._introspect.indexes(self._handle(fqn), self._identity(fqn))

    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        return self._introspect.unique_keys(self._handle(fqn), self._identity(fqn))

    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        return self._introspect.physical_layout(self._handle(fqn), self._identity(fqn))

    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]] | None:
        return self._introspect.view_dependencies(self._cursor)

    def extract_comments(self, fqn: str) -> CommentsMeta:
        return self._introspect.comments(self._handle(fqn), self._identity(fqn))

    def estimate_row_count(self, fqn: str) -> int | None:
        return row_count_or_none(
            self._introspect.row_count_estimate(self._handle(fqn), self._identity(fqn)),
        )

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, PhaseA]:
        del config

        return self._stats.compute_base(self._handle(fqn), self._identity(fqn), columns, scope)

    def compute_column_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        fk_source_columns: frozenset[str],
        *,
        suppress_values: frozenset[str] = frozenset(),
        on_column: ColumnProgress | None = None,
        scope: TableScope | None = None,
    ) -> PhaseB:
        return self._stats.compute_columns(
            self._handle(fqn),
            self._identity(fqn),
            columns,
            config,
            counts,
            base,
            fk_source_columns,
            suppress_values,
            on_column,
            scope,
        )

    def profile_parts(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        scope: TableScope | None = None,
    ) -> dict[str, ColumnParts]:
        from .identifiers import source_column

        if not any(
            hasattr(self._stats, reads) for reads in ("ARRAYS", "RECORDS", "MAPS", "DOCUMENTS")
        ):
            return {}

        del counts
        cursor = self._handle(fqn)
        identity = self._identity(fqn)
        source = self._stats.table_source(identity, scope)
        root_type = getattr(self._stats, "root_type", lambda col: col.classified_type)
        out: dict[str, ColumnParts] = {}

        for col in columns:
            root = PartSource("", root_type(col), source, source_column(col, self.DIALECT))

            if self._holds_parts(cursor, root):
                out[col.name] = self._descended(cursor, identity, root, config)

        return out

    def _descended(
        self,
        cursor: Any,
        identity: Identity,
        root: PartSource,
        config: StatisticsConfig,
    ) -> ColumnParts:
        from . import statements

        execute = partial(self._driver.exec_query, cursor)
        shape = self._shape(cursor, root, column=True)
        found = descend(
            root,
            config,
            children=partial(self._children, cursor, config),
            occurrences=lambda node: statements.row_count_of(execute, node.source),
            profile=partial(self._profiled_part, cursor, identity, config),
            size=shape.size if shape is not None else None,
        )

        if shape is None:
            return found

        return replace(
            found,
            cardinality=shape.cardinality,
            empty_count=shape.empty_count,
            norm=shape.norm,
            zero_count=shape.zero_count,
        )

    def document_types(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        scope: TableScope | None = None,
    ) -> dict[str, tuple[tuple[str, int], ...]]:
        from .identifiers import source_column

        documents: DocumentReads | None = getattr(self._stats, "DOCUMENTS", None)

        if documents is None:
            return {}

        cursor = self._handle(fqn)
        source = self._stats.table_source(self._identity(fqn), scope)
        root_type = getattr(self._stats, "root_type", lambda col: col.classified_type)

        return {
            col.name: self._types(cursor, documents, source, source_column(col, self.DIALECT))
            for col in columns
            if documents.holds(PartSource("", root_type(col), source))
        }

    def _types(
        self,
        cursor: Any,
        documents: DocumentReads,
        source: str,
        operand: str,
    ) -> tuple[tuple[str, int], ...]:
        from .sql_layout import select_from

        kind = documents.type_of(operand)
        rows = self._driver.exec_query(
            cursor,
            select_from([f"{kind} AS t", "COUNT(1) AS n"], source)
            + f"\nWHERE\n  {operand} IS NOT NULL\nGROUP BY\n  {kind}",
        ).fetchall()

        return tuple((str(name), int(count)) for name, count in rows if name is not None)

    def _holds_parts(self, cursor: Any, node: PartSource) -> bool:
        documents: DocumentReads | None = getattr(self._stats, "DOCUMENTS", None)

        return (
            self._element_type(cursor, node) is not None
            or bool(self._members(cursor, node))
            or self._entries(cursor, node) is not None
            or (documents is not None and documents.holds(node))
        )

    def _shape(self, cursor: Any, node: PartSource, *, column: bool) -> InstanceShape | None:
        from . import statements

        execute = partial(self._driver.exec_query, cursor)
        element = self._element_type(cursor, node)
        arrays: ArrayReads | None = getattr(self._stats, "ARRAYS", None)

        if element is not None and arrays is not None:
            floating = column and is_floating_type(element)

            return statements.instance_shape(
                execute,
                node,
                arrays.size,
                distinct=arrays.distinct if column else None,
                norm=arrays.norm if floating else None,
            )

        entries = self._entries(cursor, node)

        if entries is not None:
            return statements.instance_shape(execute, node, lambda _operand: entries.size)

        documents: DocumentReads | None = getattr(self._stats, "DOCUMENTS", None)

        if documents is not None and documents.holds(node):
            return statements.instance_shape(execute, node, documents.size)

        return None

    def _element_type(self, cursor: Any, node: PartSource) -> str | None:
        if not hasattr(self._stats, "ARRAYS"):
            return None

        probe = getattr(self._stats, "element_type_of", None)

        return probe(cursor, node) if probe is not None else element_type(node.sql_type)

    def _entries(self, cursor: Any, node: PartSource) -> MapEntries | None:
        maps: MapReads | None = getattr(self._stats, "MAPS", None)

        return maps.entries(cursor, node) if maps is not None else None

    def _members(self, cursor: Any, node: PartSource) -> list[Member]:
        records: RecordReads | None = getattr(self._stats, "RECORDS", None)
        documents: DocumentReads | None = getattr(self._stats, "DOCUMENTS", None)

        if records is None or (documents is not None and documents.holds(node)):
            return []

        members = records.members(cursor, node)
        names = [m.name for m in members]

        # A name declared twice cannot be addressed by a path or an accessor, so neither copy is.
        return [m for m in members if names.count(m.name) == 1]

    def _children(
        self,
        cursor: Any,
        config: StatisticsConfig,
        node: PartSource,
    ) -> list[PartSource]:
        from .identifiers import source_column

        value = source_column(_PART_VALUE, self.DIALECT)
        element = self._element_type(cursor, node)
        arrays: ArrayReads | None = getattr(self._stats, "ARRAYS", None)

        if element is not None and arrays is not None:
            return [
                PartSource(f"{node.path}{ELEMENT}", element, arrays.elements(node, element), value),
            ]

        entries = self._entries(cursor, node)

        if entries is not None:
            return self._map_children(cursor, config, node, entries)

        documents: DocumentReads | None = getattr(self._stats, "DOCUMENTS", None)

        if documents is not None and documents.holds(node):
            return self._document_children(cursor, config, node, documents)

        records: RecordReads | None = getattr(self._stats, "RECORDS", None)

        return [
            PartSource(
                f"{node.path}{member_step(m.name)}",
                m.sql_type,
                records.source(node, m),
                value,
                held=True,
            )
            for m in self._members(cursor, node)
            if records is not None
        ]

    def _map_children(
        self,
        cursor: Any,
        config: StatisticsConfig,
        node: PartSource,
        entries: MapEntries,
    ) -> list[PartSource]:
        from . import statements
        from .identifiers import SOURCE_ALIAS, source_column
        from .sql_layout import derived, select_from

        maps: MapReads = self._stats.MAPS
        value = source_column(_PART_VALUE, self.DIALECT)
        execute = partial(self._driver.exec_query, cursor)
        keys, distinct = statements.map_keys(
            execute,
            self.DIALECT,
            entries.source,
            config.max_parts,
        )
        key_set = PartSource(
            f"{node.path}{KEYS}",
            entries.key_sql_type,
            derived(select_from(["ent.k AS v"], entries.source), SOURCE_ALIAS),
            value,
            unlisted=distinct - len(keys),
        )
        held = [
            PartSource(
                f"{node.path}{member_step(scalar_text(key))}",
                entries.value_sql_type,
                derived(
                    select_from(["ent.v AS v"], entries.source)
                    + f"\nWHERE\n  ent.k = {maps.literal(key, entries.key_sql_type)}",
                    SOURCE_ALIAS,
                ),
                value,
                keyed=True,
            )
            for key, _ in keys
        ]

        return [key_set, *held]

    def _document_children(
        self,
        cursor: Any,
        config: StatisticsConfig,
        node: PartSource,
        documents: DocumentReads,
    ) -> list[PartSource]:
        from . import statements
        from .identifiers import SOURCE_ALIAS, source_column
        from .sql_layout import derived, select_from

        value = source_column(_PART_VALUE, self.DIALECT)
        execute = partial(self._driver.exec_query, cursor)
        entries = documents.entries(node)
        keys, distinct = statements.map_keys(execute, self.DIALECT, entries, config.max_parts)
        held = [
            derived(
                select_from(["ent.v AS v"], entries)
                + f"\nWHERE\n  ent.k = {documents.literal(key, documents.key_sql_type)}",
                "ent",
            )
            for key, _ in keys
        ]
        children = (
            [
                PartSource(
                    f"{node.path}{KEYS}",
                    documents.key_sql_type,
                    derived(select_from(["ent.k AS v"], entries), SOURCE_ALIAS),
                    value,
                    unlisted=distinct - len(keys),
                ),
            ]
            if distinct
            else []
        )
        children += [
            self._document_value(
                cursor,
                documents,
                f"{node.path}{member_step(scalar_text(key))}",
                values,
                keyed=True,
            )
            for (key, _), values in zip(keys, held, strict=True)
        ]

        return [
            *children,
            self._document_value(
                cursor,
                documents,
                f"{node.path}{ELEMENT}",
                documents.elements(node),
                keyed=False,
            ),
        ]

    def _document_value(
        self,
        cursor: Any,
        documents: DocumentReads,
        path: str,
        values: str,
        *,
        keyed: bool,
    ) -> PartSource:
        from .identifiers import SOURCE_ALIAS, source_column, string_literal
        from .sql_layout import derived, select_from

        kind = documents.type_of("ent.v")
        rows = self._driver.exec_query(
            cursor,
            select_from([f"DISTINCT {kind} AS t"], values),
        ).fetchall()
        sql_type = documents.value_type(
            str(name) for (name,) in rows if name is not None and name != documents.null_name
        )
        read = (
            f"CASE WHEN {kind} = {string_literal(documents.null_name)} THEN NULL "
            f"ELSE {documents.read('ent.v', sql_type)} END AS v"
        )

        return PartSource(
            path,
            sql_type,
            derived(select_from([read], values), SOURCE_ALIAS),
            source_column(_PART_VALUE, self.DIALECT),
            held=True,
            keyed=keyed,
        )

    def _profiled_part(
        self,
        cursor: Any,
        identity: Identity,
        config: StatisticsConfig,
        node: PartSource,
        occurrences: int,
    ) -> PartStats:
        from . import statements

        column = replace(_PART_VALUE, sql_type=node.sql_type)
        execute = partial(self._driver.exec_query, cursor)

        # A record is read only through its members; as a value it publishes its count profile.
        if self._members(cursor, node):
            nulls = statements.row_count_of(execute, node.source) - statements.row_count_of(
                execute,
                f"{node.source}\nWHERE\n  {self._present(node.operand)}",
            )

            return PartStats(
                path=node.path,
                occurrences=occurrences,
                stats=ColumnStats(
                    sql_type=node.sql_type,
                    nullable=True,
                    null_count=nulls,
                    null_rate=0.0,
                    cardinality=None,
                    cardinality_ratio=None,
                    cardinality_method=None,
                ),
                keyed=node.keyed,
            )

        pooled = node.path.endswith(ELEMENT) and is_floating_type(node.sql_type)
        stats = self._stats.profile_part(
            cursor,
            identity.with_columns([column]),
            node.source,
            column,
            config,
            suppress_values=frozenset({column.name}) if pooled else frozenset(),
        )
        shape = self._shape(cursor, node, column=False)
        documents: DocumentReads | None = getattr(self._stats, "DOCUMENTS", None)
        document = documents is not None and documents.holds(node)

        if document and base_type(node.sql_type) != documents.array_name.lower():
            stats = replace(
                stats,
                types=self._types(cursor, documents, node.source, node.operand),
            )
        elif shape is not None:
            stats = replace(stats, empty_count=shape.empty_count)

        return PartStats(
            path=node.path,
            occurrences=occurrences,
            stats=stats,
            samples=tuple(self._part_samples(cursor, identity, node, config)),
            size=shape.size if shape is not None else None,
            keyed=node.keyed,
        )

    def _present(self, operand: str) -> str:
        records: RecordReads | None = getattr(self._stats, "RECORDS", None)

        return records.present(operand) if records is not None else f"{operand} IS NOT NULL"

    def _part_samples(
        self,
        cursor: Any,
        identity: Identity,
        node: PartSource,
        config: StatisticsConfig,
    ) -> list[Any]:
        from . import statements

        if not is_string_like_type(node.sql_type):
            return []

        return statements.distinct_values(
            partial(self._driver.exec_query, cursor),
            self.DIALECT,
            node.source,
            node.operand,
            self._stats.render_text(node.operand, node.sql_type),
            config.looks_like_sample_size,
            statements.table_seed(identity),
        )

    def compute_null_patterns(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        scope: TableScope | None = None,
    ) -> NullPatterns | None:
        return self._stats.compute_null_patterns(
            self._handle(fqn),
            self._identity(fqn),
            columns,
            config,
            counts,
            base,
            scope,
        )

    def probe_grain(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        candidates: tuple[tuple[str, str], ...],
        scope: TableScope | None = None,
    ) -> tuple[tuple[str, str], ...]:
        return self._stats.probe_grain(
            self._handle(fqn),
            self._identity(fqn),
            columns,
            counts,
            candidates,
            scope,
        )

    def probe_timeline(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        column: str,
        unit: Literal["day", "week", "month"],
        scope: TableScope | None = None,
    ) -> tuple[tuple[str, int], ...]:
        return self._stats.probe_timeline(
            self._handle(fqn),
            self._identity(fqn),
            columns,
            counts,
            column,
            unit,
            scope,
        )

    def compute_populated_windows(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        anchor_column: str,
        subject_columns: tuple[str, ...],
        scope: TableScope | None = None,
    ) -> dict[str, tuple[str, str]]:
        return self._stats.compute_populated_windows(
            self._handle(fqn),
            self._identity(fqn),
            columns,
            counts,
            anchor_column,
            subject_columns,
            scope,
        )

    def probe_dependencies(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        base: dict[str, BaseStats],
        candidates: tuple[tuple[str, str], ...],
        scope: TableScope | None = None,
    ) -> dict[tuple[str, str], float]:
        return self._stats.probe_dependencies(
            self._handle(fqn),
            self._identity(fqn),
            columns,
            counts,
            base,
            candidates,
            scope,
        )

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        return self._stats.materialize(self._handle(fqn), self._identity(fqn), scope)

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        self._stats.release(self._handle(fqn), scope)

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
        sql_type: str | None = None,
    ) -> list[Any]:
        return self._looks_like.sample_distinct(
            self._handle(fqn),
            self._read_identity(fqn),
            column,
            n,
            scope,
            sql_type,
        )

    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: SketchKind,
        k: int,
    ) -> tuple[int, ...]:
        return tuple(self._key_sketch(fqn, column, sql_type, kind, k))

    def compute_normalized_cardinality(
        self,
        fqn: str,
        column: str,
        scope: TableScope | None = None,
    ) -> int:
        return self._normalization.compute_normalized_cardinality(
            self._handle(fqn),
            self._read_identity(fqn),
            column,
            scope,
        )

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run user-authored SQL and return all rows; SQL assertion path (ASSERTIONS.md 3).

        Read-only is the operator's responsibility, not enforced here (ASSERTIONS.md 3.4).
        """

        rows = self._driver.exec_query(self._cursor, sql).fetchall()

        return [tuple(row) for row in rows]

    def _key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: SketchKind,
        k: int,
        *,
        order: str = "h",
    ) -> list[int]:
        from . import statements

        identity = self._read_identity(fqn)
        quoted_col, canonical = self._sketch.canonical_value(identity, column, sql_type, kind)

        return statements.key_sketch(
            partial(self._driver.exec_query, self._handle(fqn)),
            identity.quoted(),
            quoted_col,
            canonical,
            self._sketch.low64_expr("dst.v"),
            k,
            order=order,
        )

    def _identity(self, fqn: str) -> Identity:
        return self._identities[fqn]

    def _read_identity(self, fqn: str) -> Identity:
        return self._identity(fqn)

    def _handle(self, fqn: str) -> Any:
        del fqn

        return self._cursor

    @property
    def _cursor(self) -> Any:
        if not self._connection.is_open():
            raise self._not_connected("adapter is not connected; call connect() first")

        return self._connection.cursor


class PerDatabaseSqlAdapter(SqlAdapter):
    """A SqlAdapter holding one session per database, opened on first use and kept for the run.

    A subclass names `_unopened(database)` and passes its entry database to `_start_sessions`.
    """

    _entry_database: str
    _sessions: dict[str, Any]

    def close(self) -> None:
        for session in self._sessions.values():
            session.close()

        self._sessions = {self._entry_database: self._connection}

    def new_session(self) -> Self:
        session = super().new_session()
        session._sessions = {self._entry_database: session._connection}

        return session

    @abstractmethod
    def _unopened(self, database: str) -> Any: ...

    def _start_sessions(self, entry_database: str) -> None:
        self._entry_database = entry_database
        self._connection = self._unopened(entry_database)
        self._sessions = {entry_database: self._connection}

    def _session(self, database: str) -> Any:
        session = self._sessions.get(database)

        if session is None:
            session = self._unopened(database)
            session.open()
            self._sessions[database] = session

        return session


def _length(col: ColumnMeta, base: BaseStats, reads: ColumnReads) -> Length | None:
    if base.length_min is None or base.length_max is None:
        return None

    if reads.length_p95 is None:
        p95 = round_statistic(base.length_p95)
    elif (p95 := reads.length_p95(col)) is None:
        return None

    return Length(
        min=base.length_min,
        max=base.length_max,
        avg=round_statistic(base.length_avg),
        p95=p95,
    )


def _settle_near_unique(
    columns: list[ColumnMeta],
    stats: dict[str, BaseStats],
    rows_scanned: int,
    recount: Callable[[list[ColumnMeta]], Sequence[Any] | None],
) -> Exception | None:
    near_unique = [
        column
        for column in columns
        if (base := stats.get(column.name)) is not None
        and base.supported
        and rows_scanned
        and base.cardinality / rows_scanned >= EXACT_PROBE_RATIO
    ]

    if not near_unique:
        return None

    try:
        row = recount(near_unique)
    except Exception as exc:  # noqa: BLE001 - the approximate counts stand, and say so
        return exc

    if row is None:
        return None

    for column, value in zip(near_unique, row, strict=True):
        base = stats[column.name]
        # A separate statement may read a different snapshot than phase A, so clamp to non_null.
        stats[column.name] = replace(
            base,
            cardinality=min(rows_scanned - base.null_count, int(value)),
            cardinality_method="exact",
        )

    return None


def _described_apart(column: ColumnMeta) -> bool:
    return is_spatial_type(column.classified_type) or is_vector_type(column.classified_type)


_PART_VALUE = ColumnMeta(name="v", sql_type="", nullable=True, default=None, ordinal=1)
