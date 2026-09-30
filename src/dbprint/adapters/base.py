"""Adapter ABC + intermediate dataclass types. See ARCHITECTURE.md 2.

Adapters return typed intermediate records; the engine converts them into on-disk artifacts.
`StatisticsConfig`, `Distribution` and `FreshnessClassification` are re-exported here;
`Freshness` is engine-derived, never adapter output.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Literal, Self

from dbprint.config import StatisticsConfig
from dbprint.spec.classification import base_type, has_day_resolution, is_recognised_type
from dbprint.spec.coverage import coverage_share
from dbprint.spec.distribution import Distribution, Frequencies
from dbprint.spec.rounding import UnrepresentableValue
from dbprint.spec.sketch import SketchKind
from dbprint.spec.temporal_age import FreshnessClassification
from dbprint.spec.value_text import scalar_text, value_order_key


__all__ = [
    "MIN_SAMPLE_DRAW",
    "PHASE_A_EXPRESSION_BUDGET",
    "Adapter",
    "AdapterType",
    "BaseStats",
    "ColumnMeta",
    "ColumnProgress",
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
    "PhysicalLayout",
    "PhysicalLayoutKey",
    "Range",
    "SketchKind",
    "StatisticsConfig",
    "TableCounts",
    "TableMeta",
    "TableScope",
    "TableType",
    "TemporalShape",
    "UniqueKeyMeta",
    "ValueCount",
    "batched_by_cost",
    "has_measurable_nulls",
    "materialized_name",
    "null_flags",
    "null_patterns_from_rows",
    "row_count_or_none",
    "seed_from_fqn",
    "temporal_block_unmeasured",
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
    """Identifies a table/view/matview and its namespace position."""

    fqn: str
    type: TableType
    namespace_path: tuple[str, ...]


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
    serialized, since SPEC 2.2.8 forbids recording how the sample was drawn.
    """

    sample: float | None = None
    filter: str | None = None
    materialized: str | None = None

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

    Every `declines` column is null-counted in one statement and reported unsupported.
    """

    rows_scanned: int | None = None
    stats: dict[str, BaseStats] = {}
    unmeasured: dict[str, int] = {}
    failures: list[Exception] = []
    declined = [c for c in columns if declines(c)]
    columns = [c for c in columns if not declines(c)]

    if declined:
        rows_scanned, nulls = null_counts(declined)
        stats.update(
            (
                column.name,
                BaseStats(
                    null_count=min(nulls[column.name], rows_scanned),
                    cardinality=0,
                    cardinality_method="exact",
                    supported=False,
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
