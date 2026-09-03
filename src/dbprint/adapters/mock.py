"""Deterministic in-memory adapter for engine tests. See ARCHITECTURE.md 2 (Mock adapter).

Constructed from a dict keyed by FQN; each method returns its fixture content verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Literal

from dbprint.config import StatisticsConfig
from dbprint.config.selectors import expand
from dbprint.spec.sketch import SketchKind, canonical_form, low64_md5
from .base import (
    Adapter,
    BaseStats,
    ColumnMeta,
    ColumnProgress,
    ColumnStats,
    CommentsMeta,
    ForeignKeyMeta,
    IndexMeta,
    NullPatterns,
    PhysicalLayout,
    RowCountMethod,
    TableCounts,
    TableMeta,
    TableScope,
    TableType,
    UniqueKeyMeta,
)


__all__ = ["MockAdapter", "MockTable"]


@dataclass(frozen=True)
class MockTable:
    """All data the mock adapter holds for one FQN."""

    type: TableType
    namespace_path: tuple[str, ...]
    ddl: str
    columns: list[ColumnMeta]
    relationships: list[ForeignKeyMeta]
    indexes: list[IndexMeta]
    comments: CommentsMeta
    stats: dict[str, ColumnStats]
    samples: dict[str, list[Any]]
    # Stated, never derived: the fixture holds per-column counts, not rows, so co-nullity
    # is not recoverable from it.
    null_patterns: NullPatterns | None = None
    row_count: int = 0
    row_count_estimate: int | None = None
    unique_keys: list[UniqueKeyMeta] = field(default_factory=list)
    # Both default to the unnarrowed reading; a fixture sets them to exercise a narrowed read.
    rows_scanned: int | None = None
    row_count_method: RowCountMethod = "exact"
    # None (the default) means "not clustered"; a fixture states one to exercise a key.
    physical_layout: PhysicalLayout | None = None
    # Column pairs the fixture declares row-count-unique; stated, never derived from `stats`.
    measured_unique_pairs: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    # (determinant, dependent) -> strength; stated by the fixture, never derived from `stats`.
    dependency_strengths: dict[tuple[str, str], float] = field(default_factory=dict)
    # column -> trimmed/case-folded distinct count; stated, never derived from `stats`.
    # A column absent here defaults to its own `cardinality` (no case/whitespace merges).
    normalized_cardinalities: dict[str, int] = field(default_factory=dict)
    # column -> ordered (bucket_start, count) pairs; stated, never derived from `stats` or
    # `samples` - a fixture states the buckets it wants `probe_timeline` to hand back.
    timeline_buckets: dict[str, tuple[tuple[str, int], ...]] = field(default_factory=dict)
    # subject column -> (from, to); stated, never derived - a fixture states the window it
    # wants `compute_populated_windows` to hand back.
    populated_windows: dict[str, tuple[str, str]] = field(default_factory=dict)


class MockAdapter(Adapter):
    """Adapter backed entirely by a static fixture dict."""

    # Draws nothing - every statistic is fixture-stated, so an unmaterialized sample scope
    # can never disagree with itself across statements.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = True

    def __init__(
        self,
        fixture: dict[str, MockTable],
        query_results: dict[str, list[tuple[Any, ...]]] | None = None,
        dependencies: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._fixture = dict(fixture)
        self._connected = False
        self._query_results: dict[str, list[tuple[Any, ...]]] = dict(query_results or {})
        # Connection-scoped, like `default_collation` - not per-MockTable. None (the default)
        # means "not asked"; a dict (even empty) means the catalog answered.
        self._dependencies = dependencies

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        self._require_connected()
        fqns = list(self._fixture)
        in_scope = expand(fqns, config_include=include, config_exclude=exclude)

        return [
            TableMeta(
                fqn=fqn,
                type=self._fixture[fqn].type,
                namespace_path=self._fixture[fqn].namespace_path,
            )
            for fqn in in_scope
        ]

    def extract_ddl(self, fqn: str) -> str:
        return self._lookup(fqn).ddl

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        return list(self._lookup(fqn).columns)

    def default_collation(self) -> str:
        return "C"

    def introspect_relationships(self, fqn: str) -> list[ForeignKeyMeta]:
        return list(self._lookup(fqn).relationships)

    def introspect_indexes(self, fqn: str) -> list[IndexMeta]:
        return list(self._lookup(fqn).indexes)

    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        """Declared keys the fixture states, defaulting to none; never derived from a stat."""

        return list(self._lookup(fqn).unique_keys)

    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        return self._lookup(fqn).physical_layout

    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]] | None:
        """The fixture's stated connection-wide map - no SQL, no per-view lookup."""

        return self._dependencies

    def extract_comments(self, fqn: str) -> CommentsMeta:
        return self._lookup(fqn).comments

    def estimate_row_count(self, fqn: str) -> int | None:
        """The fixture's own estimate, separate from `row_count` and defaulting to unavailable."""

        return self._lookup(fqn).row_count_estimate

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, dict[str, BaseStats]]:
        """Phase A read straight off the fixture's canned per-column statistics.

        `rows_scanned`/`row_count_method` are fixture-stated rather than derived from
        `scope`, so a test can express a narrowed exact-counted read; base facts are
        projected from the canned `ColumnStats`, so the two phases cannot disagree.
        """

        del columns, config, scope
        tbl = self._lookup(fqn)
        counts = TableCounts(
            row_count=tbl.row_count,
            rows_scanned=tbl.row_count if tbl.rows_scanned is None else tbl.rows_scanned,
            row_count_method=tbl.row_count_method,
        )
        base = {
            name: BaseStats(
                null_count=s.null_count,
                # No cardinality means unsupported; `supported=False` keeps the 0 from being read.
                cardinality=s.cardinality if s.cardinality is not None else 0,
                cardinality_method=s.cardinality_method or "exact",
                supported=s.cardinality is not None,
            )
            for name, s in tbl.stats.items()
        }

        return counts, base

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
    ) -> dict[str, ColumnStats]:
        """Phase B: the fixture verbatim, minus whatever was suppressed.

        Stats are pre-canned and never cross-checked, so config/counts/base/fk_source_columns
        are ignored; `suppress_values` and `on_column` are honoured, so a suppression test
        sees a real withheld list and the progress contract still fires per column.
        """

        del config, counts, base, fk_source_columns, scope
        tbl = self._lookup(fqn)

        if on_column is not None:
            total = len(columns)

            for index, col in enumerate(columns, start=1):
                on_column(index, total, col.name)

        return {
            name: _without_value_list(s) if name in suppress_values else s
            for name, s in tbl.stats.items()
        }

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        """Mark a sampled draw materialized, with no real copy behind it.

        `compute_normalized_cardinality` still checks `materialized`, so a caller regressing to
        the unmaterialized scope is caught there.
        """

        del fqn

        return replace(scope, materialized="mock-materialized")

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        """No real copy to drop."""

        del fqn, scope

    def compute_null_patterns(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        scope: TableScope | None = None,
    ) -> NullPatterns | None:
        del columns, config, counts, base, scope

        return self._lookup(fqn).null_patterns

    def probe_grain(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        candidates: tuple[tuple[str, str], ...],
        scope: TableScope | None = None,
    ) -> tuple[tuple[str, str], ...]:
        """Filter `candidates` against the fixture's canned unique pairs - no SQL, no arithmetic."""

        del columns, counts, scope
        unique = self._lookup(fqn).measured_unique_pairs

        return tuple(pair for pair in candidates if pair in unique or pair[::-1] in unique)

    def probe_timeline(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        column: str,
        unit: Literal["day", "week", "month"],
        scope: TableScope | None = None,
    ) -> tuple[tuple[str, int], ...]:
        """Return the fixture's canned buckets for `column` - no SQL, no truncation math."""

        del columns, counts, unit, scope

        return self._lookup(fqn).timeline_buckets.get(column, ())

    def compute_populated_windows(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        anchor_column: str,
        subject_columns: tuple[str, ...],
        scope: TableScope | None = None,
    ) -> dict[str, tuple[str, str]]:
        """Return the fixture's canned windows for `subject_columns` - no SQL, no aggregate."""

        del columns, counts, anchor_column, scope
        stated = self._lookup(fqn).populated_windows

        return {name: stated[name] for name in subject_columns if name in stated}

    def probe_dependencies(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        counts: TableCounts,
        base: dict[str, BaseStats],
        candidates: tuple[tuple[str, str], ...],
        scope: TableScope | None = None,
    ) -> dict[tuple[str, str], float]:
        """Filter `candidates` against the fixture's canned strengths - no SQL, no arithmetic."""

        del columns, counts, base, scope
        strengths = self._lookup(fqn).dependency_strengths

        return {pair: strengths[pair] for pair in candidates if pair in strengths}

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
    ) -> list[Any]:
        samples = self._lookup(fqn).samples.get(column, [])

        return list(samples[:n])

    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: SketchKind,
        k: int,
    ) -> tuple[int, ...]:
        """Hashed in Python (`spec.sketch`) from the fixture's stated `stats[column].values`.

        Empty unless `values` is exhaustive - its length equals `cardinality` (SPEC 2.2.4) -
        since a sketch over a truncated top-N list would understate containment/coverage.
        """

        del sql_type
        col = self._lookup(fqn).stats[column]
        values = col.values or ()

        if len(values) != col.cardinality:
            return ()

        hashes = sorted(low64_md5(canonical_form(v.value, kind)) for v in values)

        return tuple(hashes[:k])

    def compute_normalized_cardinality(
        self,
        fqn: str,
        column: str,
        scope: TableScope | None = None,
    ) -> int:
        """The fixture's stated merged count, defaulting to `cardinality` (no merges).

        Raises on a `sample` scope with no materialized copy - every real adapter's read refuses
        the same state, so the wrong scope is caught rather than answered anyway.
        """

        if scope is not None and scope.sample is not None and scope.materialized is None:
            raise ValueError(
                f"{fqn!r}.{column!r}: compute_normalized_cardinality was given an "
                f"unmaterialized sample scope",
            )

        tbl = self._lookup(fqn)
        stated = tbl.normalized_cardinalities.get(column)

        if stated is not None:
            return stated

        col = tbl.stats.get(column)

        return col.cardinality if col is not None and col.cardinality is not None else 0

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Return canned results matched on the exact SQL string.

        A miss raises KeyError, which the SQL assertion evaluator surfaces as
        assertion.sql-execution-error.
        """

        self._require_connected()

        if sql not in self._query_results:
            raise KeyError(f"MockAdapter: no canned result for SQL {sql!r}")

        return list(self._query_results[sql])

    # Helpers

    def _lookup(self, fqn: str) -> MockTable:
        self._require_connected()

        if fqn not in self._fixture:
            raise KeyError(f"MockAdapter: no fixture entry for FQN {fqn!r}.")

        return self._fixture[fqn]

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError("MockAdapter: call connect() before any operation.")


def _without_value_list(stats: ColumnStats) -> ColumnStats:
    """One column's stats as an adapter that skipped the enumeration returns them.

    `distribution` goes too, being derived from the value list.
    """

    return replace(stats, values=None, values_coverage=None, distribution=None)
