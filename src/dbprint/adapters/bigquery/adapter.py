"""BigqueryAdapter - concrete Adapter wired to the bigquery helper modules, built from a
credentials dict; `cursor_factory` lets tests substitute a substrate-appropriate cursor.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from . import ddl as ddl_module
from . import introspect as introspect_module
from . import looks_like as looks_like_module
from . import normalization as normalization_module
from . import sketch as sketch_module
from . import stats as stats_module
from .connection import (
    BigqueryConnectionError,
    Connection,
    ConnectionParams,
    CursorFactory,
    exec_query,
)
from .identity import Identity
from ..base import (
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
    SketchKind,
    StatisticsConfig,
    TableCounts,
    TableMeta,
    TableScope,
    UniqueKeyMeta,
)


class UnknownTable(LookupError):
    """Raised when a table's physical identifiers were never captured."""


class BigqueryAdapter(Adapter):
    """Concrete Adapter for Google BigQuery backed by google-cloud-bigquery.

    BigQuery is case-sensitive while dbprint addresses objects by lowercased paths, so the adapter
    records the physical form as `list_tables`/`introspect_columns` observe it - the first must run.
    """

    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("project", "dataset")
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = ("credentials_file",)
    # TABLESAMPLE has no seed on BigQuery, so an unmaterialized `sample` scope redraws with
    # no guarantee of agreement across statements.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = False
    # The materialized copy is a real dataset table, not a session-scoped temp table like
    # every other adapter's - it carries its own expiration instead (bigquery/stats.py).
    MATERIALIZED_SCOPE_SESSION_SCOPED: ClassVar[bool] = False

    def __init__(
        self,
        credentials: dict[str, str],
        cursor_factory: CursorFactory | None = None,
    ) -> None:
        self._params = ConnectionParams.from_credentials(credentials)
        self._connection = Connection(self._params, cursor_factory)
        # Populated by list_tables()'s own read of TABLES.ddl - extract_ddl reads from here
        # first rather than paying a second round trip for data this connection already has.
        self._ddl_cache: dict[str, str] = {}
        self._physical_tables: dict[str, str] = {}
        self._physical_columns: dict[str, dict[str, str]] = {}

    def connect(self) -> None:
        self._connection.open()

    def close(self) -> None:
        self._connection.close()

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        selected, ddl_by_fqn, physical_by_fqn = introspect_module.list_tables(
            self._cursor,
            self._params.project,
            self._params.dataset,
            include,
            exclude,
        )
        self._ddl_cache.update({fqn: ddl_module.normalize(ddl) for fqn, ddl in ddl_by_fqn.items()})
        self._physical_tables = physical_by_fqn
        self._physical_columns = {}

        return selected

    def extract_ddl(self, fqn: str) -> str:
        cached = self._ddl_cache.get(fqn)

        if cached is not None:
            return cached

        return ddl_module.extract_ddl(self._cursor, self._params.project, self._identity(fqn))

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        metas, physical = introspect_module.columns(
            self._cursor,
            self._params.project,
            self._identity(fqn),
        )
        self._physical_columns[fqn] = physical

        return metas

    def default_collation(self) -> str:
        return introspect_module.default_collation()

    def introspect_relationships(self, fqn: str) -> list[ForeignKeyMeta]:
        return introspect_module.relationships(
            self._cursor,
            self._params.project,
            self._identity(fqn),
        )

    def introspect_indexes(self, fqn: str) -> list[IndexMeta]:
        return introspect_module.indexes(self._cursor, fqn)

    def introspect_unique_keys(self, fqn: str) -> list[UniqueKeyMeta]:
        return introspect_module.unique_keys(
            self._cursor,
            self._params.project,
            self._identity(fqn),
        )

    def introspect_physical_layout(self, fqn: str) -> PhysicalLayout | None:
        return introspect_module.physical_layout(
            self._cursor,
            self._params.project,
            self._identity(fqn),
        )

    def introspect_view_dependencies(self) -> dict[str, tuple[str, ...]] | None:
        return introspect_module.view_dependencies(self._cursor)

    def extract_comments(self, fqn: str) -> CommentsMeta:
        return introspect_module.comments(self._cursor, self._params.project, self._identity(fqn))

    def estimate_row_count(self, fqn: str) -> int | None:
        return introspect_module.estimate_row_count(
            self._cursor,
            self._params.project,
            self._identity(fqn),
        )

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, dict[str, BaseStats]]:
        del config

        return stats_module.compute_base(
            self._cursor,
            self._params.project,
            self._identity(fqn),
            columns,
            scope,
        )

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
        return stats_module.compute_columns(
            self._cursor,
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

    def compute_null_patterns(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        counts: TableCounts,
        base: dict[str, BaseStats],
        scope: TableScope | None = None,
    ) -> NullPatterns | None:
        return stats_module.compute_null_patterns(
            self._cursor,
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
        return stats_module.probe_grain(
            self._cursor,
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
        return stats_module.probe_timeline(
            self._cursor,
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
        return stats_module.compute_populated_windows(
            self._cursor,
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
        return stats_module.probe_dependencies(
            self._cursor,
            self._identity(fqn),
            columns,
            counts,
            base,
            candidates,
            scope,
        )

    def materialize_scope(self, fqn: str, scope: TableScope) -> TableScope:
        return stats_module.materialize(self._cursor, self._identity(fqn), scope)

    def release_scope(self, fqn: str, scope: TableScope) -> None:
        del fqn

        stats_module.release(self._cursor, scope)

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
    ) -> list[Any]:
        return looks_like_module.sample_distinct(
            self._cursor,
            self._params.project,
            self._identity(fqn),
            column,
            n,
            scope,
        )

    def compute_key_sketch(
        self,
        fqn: str,
        column: str,
        sql_type: str,
        kind: SketchKind,
        k: int,
    ) -> tuple[int, ...]:
        return sketch_module.compute_key_sketch(
            self._cursor,
            self._identity(fqn),
            column,
            sql_type,
            kind,
            k,
        )

    def compute_normalized_cardinality(
        self,
        fqn: str,
        column: str,
        scope: TableScope | None = None,
    ) -> int:
        return normalization_module.compute_normalized_cardinality(
            self._cursor,
            self._identity(fqn),
            column,
            scope,
        )

    def execute_query(self, sql: str) -> list[tuple[Any, ...]]:
        """Run user-authored SQL and return all rows; SQL assertion path (ASSERTIONS.md 3) -
        read-only is the operator's own IAM role, not enforced here (ASSERTIONS.md 3.4).
        """

        cursor = exec_query(self._cursor, sql)
        rows = cursor.fetchall()

        return [tuple(row) for row in rows]

    def _identity(self, fqn: str) -> Identity:
        """Physical identity for a listed table - raises `UnknownTable` rather than falling back
        to the lowercased path, which would filter the catalog for a name that does not exist.
        """

        try:
            physical_table = self._physical_tables[fqn]
        except KeyError:
            raise UnknownTable(
                f"physical identifiers for {fqn!r} are unknown; "
                "call list_tables() before per-table extraction",
            ) from None

        return Identity(
            parts=(self._params.dataset, physical_table),
            columns=self._physical_columns.get(fqn, {}),
        )

    @property
    def _cursor(self) -> Any:
        if not self._connection.is_open():
            raise BigqueryConnectionError("adapter is not connected; call connect() first")

        return self._connection.cursor
