"""BigqueryAdapter - concrete Adapter wired to the bigquery helper modules, built from a
credentials dict; `cursor_factory` lets tests substitute a substrate-appropriate cursor.
"""

from __future__ import annotations

from typing import Any, ClassVar

from . import connection as connection_module
from . import ddl as ddl_module
from . import introspect as introspect_module
from . import looks_like as looks_like_module
from . import normalization as normalization_module
from . import sketch as sketch_module
from . import stats as stats_module
from .connection import (
    DIALECT,
    BigqueryConnectionError,
    Connection,
    ConnectionParams,
    DatasetLister,
    default_dataset_lister,
)
from ..base import (
    ColumnMeta,
    CommentsMeta,
    ForeignKeyMeta,
    IndexMeta,
    PhaseA,
    PhysicalLayout,
    SketchKind,
    SkippedNamespace,
    SqlAdapter,
    StatisticsConfig,
    TableCounts,
    TableMeta,
    TableScope,
    UniqueKeyMeta,
)
from ..dialect import Dialect
from ..driver import CursorFactory
from ..errors import QueryFailed
from ..identifiers import IdentityRegistry


class BigqueryAdapter(SqlAdapter):
    """Concrete Adapter for Google BigQuery backed by google-cloud-bigquery.

    BigQuery is case-sensitive while dbprint addresses objects by lowercased paths, so the adapter
    records the physical form as `list_tables`/`introspect_columns` observe it - the first must run.
    """

    KNOWN_TYPES: ClassVar[tuple[str, ...]] = stats_module.KNOWN_TYPES
    REQUIRED_KEYS: ClassVar[tuple[str, ...]] = ("project",)
    OPTIONAL_KEYS: ClassVar[tuple[str, ...]] = ("dataset", "credentials_file")
    PATH_KEYS: ClassVar[tuple[str, ...]] = ("credentials_file",)
    # TABLESAMPLE has no seed on BigQuery, so an unmaterialized `sample` scope redraws with
    # no guarantee of agreement across statements.
    SAMPLE_FALLBACK_COHERENT: ClassVar[bool] = False
    # The materialized copy is a real dataset table, not a session-scoped temp table like
    # every other adapter's - it carries its own expiration instead (bigquery/stats.py).
    MATERIALIZED_SCOPE_SESSION_SCOPED: ClassVar[bool] = False

    DIALECT: ClassVar[Dialect] = DIALECT
    _driver = connection_module
    _introspect = introspect_module
    _ddl = ddl_module
    _stats = stats_module
    _looks_like = looks_like_module
    _sketch = sketch_module
    _normalization = normalization_module
    _not_connected = BigqueryConnectionError

    def __init__(
        self,
        credentials: dict[str, str],
        cursor_factory: CursorFactory[ConnectionParams] | None = None,
        *,
        statement_timeout: int | None = None,
        dataset_lister: DatasetLister | None = None,
    ) -> None:
        self._params = ConnectionParams.from_credentials(
            credentials,
            statement_timeout=statement_timeout,
        )
        self._connection = Connection(self._params, cursor_factory)
        # Populated by list_tables()'s own read of TABLES.ddl - extract_ddl reads from here
        # first rather than paying a second round trip for data this connection already has.
        self._ddl_cache: dict[str, str] = {}
        self._dataset_lister = dataset_lister or default_dataset_lister
        self._identities = IdentityRegistry(DIALECT)
        self._skipped: tuple[SkippedNamespace, ...] = ()
        self._external: frozenset[str] = frozenset()

    def list_tables(self, include: list[str], exclude: list[str]) -> list[TableMeta]:
        datasets = (
            [self._params.dataset]
            if self._params.dataset is not None
            else self._dataset_lister(self._params)
        )
        selected, ddl_by_fqn, self._skipped = introspect_module.list_tables(
            self._cursor,
            self._params.project,
            datasets,
            include,
            exclude,
        )
        self._ddl_cache.update({fqn: ddl_module.normalize(ddl) for fqn, ddl in ddl_by_fqn.items()})
        self._identities.register(selected)
        self._external = frozenset(meta.fqn for meta, _ in selected if meta.external)

        return [meta for meta, _ in selected]

    def skipped_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return self._skipped

    def extract_ddl(self, fqn: str) -> str:
        cached = self._ddl_cache.get(fqn)

        if cached is not None:
            return cached

        return ddl_module.extract_ddl(self._cursor, self._params.project, self._identity(fqn))

    def introspect_columns(self, fqn: str) -> list[ColumnMeta]:
        metas = introspect_module.columns(self._cursor, self._params.project, self._identity(fqn))
        self._identities.attach(fqn, metas)

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

    def extract_comments(self, fqn: str) -> CommentsMeta:
        return introspect_module.comments(self._cursor, self._params.project, self._identity(fqn))

    def estimate_row_count(self, fqn: str) -> int | None:
        try:
            return introspect_module.estimate_row_count(
                self._cursor,
                self._params.project,
                self._identity(fqn),
            )
        except QueryFailed as exc:
            # BigQuery keeps no storage statistics for an external table, so a refusal is no estimate.
            if fqn not in self._external or exc.timed_out:
                raise

            return None

    def compute_base_statistics(
        self,
        fqn: str,
        columns: list[ColumnMeta],
        config: StatisticsConfig,
        scope: TableScope | None = None,
    ) -> tuple[TableCounts, PhaseA]:
        del config

        return stats_module.compute_base(
            self._cursor,
            self._params.project,
            self._identity(fqn),
            columns,
            scope,
        )

    def sample_values(
        self,
        fqn: str,
        column: str,
        n: int,
        scope: TableScope | None = None,
        sql_type: str | None = None,
    ) -> list[Any]:
        return looks_like_module.sample_distinct(
            self._cursor,
            self._params.project,
            self._identity(fqn),
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
        """`h` is a signed INT64 over the full unsigned pattern, so `(h < 0), h` sorts it unsigned."""

        hashes = self._key_sketch(fqn, column, sql_type, kind, k, order="(h < 0), h")

        return tuple(sketch_module.unsigned(h) for h in hashes)
