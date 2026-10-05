"""BigQuery-specific behaviors the contract suite's default fixtures do not exercise - run
against the emulator through a REST cursor, which reports no partitioning and no clustering.
"""

from __future__ import annotations

import pytest

from dbprint.adapters import BigqueryAdapter, StatisticsConfig
from dbprint.adapters.bigquery import DIALECT, introspect
from dbprint.adapters.errors import QueryFailed
from dbprint.adapters.identifiers import IdentifierRejected, Identity
from dbprint.spec.sketch import low64_md5
from tests.adapters.conftest import StubCursor


_PROJECT = "dbprint-test"


def _bigquery_adapter(cursor, dataset: str) -> BigqueryAdapter:
    adapter = BigqueryAdapter(
        {"project": _PROJECT, "dataset": dataset},
        cursor_factory=lambda _params: cursor,
    )
    adapter.connect()

    return adapter


class TestKeySketch:
    """The full 64-bit `spec.sketch.low64_md5` every other adapter here reproduces exactly -
    the two 32-bit halves recombined in NUMERIC, matching `redshift/sketch.py`'s shape.
    """

    def test_matches_the_shared_spec_definition(self, bigquery_test_dataset) -> None:
        cursor, dataset = bigquery_test_dataset
        values = [f"value-{i}" for i in range(200)]
        rows = ", ".join(f"('{v}')" for v in values)
        cursor.execute(f"CREATE TABLE `{dataset}`.hashed (v STRING)")
        cursor.execute(f"INSERT INTO `{dataset}`.hashed (v) VALUES {rows}")

        adapter = _bigquery_adapter(cursor, dataset)

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "hashed"
            )
            adapter.introspect_columns(table.fqn)  # populates Identity's physical column map
            sketch = adapter.compute_key_sketch(table.fqn, "v", "string", "text", k=1000)
        finally:
            adapter.close()

        expected = sorted(low64_md5(v) for v in values)

        # A hash with the top bit set is what a signed or dropped-half recombination gets wrong.
        assert any(h >= 2**63 for h in expected)
        assert list(sketch) == expected


class TestNoViewDependencies:
    """BigQuery publishes no view-dependency catalog - `INFORMATION_SCHEMA.VIEWS` carries only the
    query text, so the adapter omits the key rather than parse a guess out of it.
    """

    def test_view_dependencies_is_always_none(self, bigquery_test_dataset) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.base_table (a INT64)")
        cursor.execute(f"CREATE VIEW `{dataset}`.a_view AS SELECT a FROM `{dataset}`.base_table")
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            deps = adapter.introspect_view_dependencies()
        finally:
            adapter.close()

        assert deps is None


class TestDefaultCollation:
    def test_reports_bigquerys_own_documented_default(self, bigquery_test_dataset) -> None:
        """A constant, not a query - BigQuery has no dataset-level default to read at all, and the
        BigQuery documentation gives binary whenever a column's own `COLLATE` is unassigned.
        """

        cursor, dataset = bigquery_test_dataset
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            collation = adapter.default_collation()
        finally:
            adapter.close()

        assert collation == "binary"


class TestColumnsWithNoCollationColumn:
    """The emulator's own `INFORMATION_SCHEMA.COLUMNS` carries no `collation_name` (measured) -
    the same retry-without-the-column shape `physical_layout()` already uses elsewhere here.
    """

    def test_columns_still_resolve_with_collation_none(self, bigquery_test_dataset) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.no_collation_catalog (a STRING)")
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            table = _table(adapter, dataset, "no_collation_catalog")
            columns = adapter.introspect_columns(table.fqn)
        finally:
            adapter.close()

        assert columns[0].collation is None


class TestListTablesFiltersByFullyQualifiedName:
    """`include` patterns match against `dataset.table`, not the bare table name - a pattern
    requiring the dot (`*.name`) must therefore match.
    """

    def test_a_dotted_suffix_pattern_matches_the_qualified_name(
        self,
        bigquery_test_dataset,
    ) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.glob_target (a INT64)")
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            tables = adapter.list_tables(include=["*.glob_target"], exclude=[])
        finally:
            adapter.close()

        assert [t.fqn.split(".")[-1] for t in tables] == ["glob_target"]


class TestScratchTableExclusion:
    """`materialize()`'s copy is a real dataset table (no session-scoped relation to put it
    in), so it must be excluded by name rather than left to be profiled as a user table.
    """

    def test_a_dbprint_sample_named_table_is_never_listed(self, bigquery_test_dataset) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.dbprint_sample_deadbeef00000000 (a INT64)")
        cursor.execute(f"CREATE TABLE `{dataset}`.ordinary_table (a INT64)")
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            tables = adapter.list_tables(include=["*"], exclude=[])
        finally:
            adapter.close()

        names = [t.fqn.split(".")[-1] for t in tables]
        assert "ordinary_table" in names
        assert "dbprint_sample_deadbeef00000000" not in names


class TestRowCountEstimateAddress:
    """Where the row-count estimate is read from, and what happens when that read fails.

    Asserted on the emitted statement: the emulator has no `PARTITIONS` view and no credentials.
    """

    def test_it_reads_the_dataset_qualified_partitions_view(self) -> None:
        cursor = StubCursor([(1234,)])

        estimate = introspect.estimate_row_count(
            cursor,
            _PROJECT,
            Identity.of(("seedbank", "accession"), DIALECT),
        )

        assert estimate == 1234
        sql = cursor.statements[0]
        assert f"`{_PROJECT}`.`seedbank`.INFORMATION_SCHEMA.PARTITIONS" in sql
        # The vendor documents no dataset-qualified `TABLE_STORAGE`, and a region qualifier
        # would scope the read to the project rather than to this table's own dataset.
        assert "TABLE_STORAGE" not in sql
        assert "region-" not in sql

    def test_it_sums_the_tables_partitions(self) -> None:
        """The estimate is a sum over partition rows, never a read of one of them."""

        cursor = StubCursor([(9,)])

        introspect.estimate_row_count(
            cursor,
            _PROJECT,
            Identity.of(("seedbank", "accession"), DIALECT),
        )

        assert "SUM(prt.total_rows)" in cursor.statements[0]

    def test_a_table_with_no_partition_row_has_no_estimate(self) -> None:
        """SPEC-independent, but the engine's own reading: absent is not zero."""

        cursor = StubCursor([(None,)])

        assert (
            introspect.estimate_row_count(
                cursor,
                _PROJECT,
                Identity.of(("seedbank", "accession"), DIALECT),
            )
            is None
        )

    def test_a_refused_read_reaches_the_caller(self) -> None:
        """Swallowing it makes a missing grant indistinguishable from an empty table."""

        with pytest.raises(QueryFailed):
            introspect.estimate_row_count(
                StubCursor(
                    error=RuntimeError(
                        "Access Denied: Table dbprint-test:seedbank.INFORMATION_SCHEMA.PARTITIONS",
                    ),
                ),
                _PROJECT,
                Identity.of(("seedbank", "accession"), DIALECT),
            )


class TestMixedCaseIdentifiers:
    """BigQuery is case-sensitive throughout; the print stays lowercase (SPEC 1.3, 2.2.1)."""

    def test_a_mixed_case_table_gets_a_lowercase_path_and_is_still_queried_by_its_real_name(
        self,
        bigquery_test_dataset,
    ) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.`Accession` (a INT64)")
        cursor.execute(f"INSERT INTO `{dataset}`.`Accession` (a) VALUES (1), (2), (3)")
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "accession"
            )

            assert table.fqn == f"{dataset}.accession"
            assert table.namespace_path == (dataset, "accession")

            columns = adapter.introspect_columns(table.fqn)
            counts, _base = adapter.compute_base_statistics(table.fqn, columns, StatisticsConfig())
        finally:
            adapter.close()

        assert counts.row_count == 3

    def test_a_mixed_case_column_carries_its_physical_name(self, bigquery_test_dataset) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.mixed_col (`herbariumId` STRING, email STRING)")
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "mixed_col"
            )
            by_name = {c.name: c for c in adapter.introspect_columns(table.fqn)}
        finally:
            adapter.close()

        assert by_name["herbariumid"].physical_name == "herbariumId"
        assert by_name["email"].physical_name is None

    def test_exclude_matches_a_mixed_case_table_by_its_lowercased_fqn(
        self,
        bigquery_test_dataset,
    ) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.`Accession` (a INT64)")
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            tables = adapter.list_tables(include=["*"], exclude=[f"{dataset}.accession"])
        finally:
            adapter.close()

        assert not any(t.fqn.split(".")[-1] == "accession" for t in tables)

    def test_a_case_collision_refuses_the_run(self) -> None:
        rows: list[tuple[object, ...]] = [
            ("Coll", "BASE TABLE", None),
            ("coll", "BASE TABLE", None),
        ]
        adapter = _bigquery_adapter(StubCursor(rows), "seedbank")

        try:
            with pytest.raises(IdentifierRejected, match="case-collides-with"):
                adapter.list_tables(include=["*"], exclude=[])
        finally:
            adapter.close()


class TestTableKinds:
    """`INFORMATION_SCHEMA.TABLES.table_type` values listed as tables, and the DDL they keep."""

    def test_a_clone_is_listed_as_a_table_with_its_catalog_ddl(self) -> None:
        rows: list[tuple[object, ...]] = [
            ("accession", "BASE TABLE", "CREATE TABLE seedbank.accession (a INT64);"),
            ("accession_copy", "CLONE", "CREATE TABLE seedbank.accession_copy CLONE x;"),
        ]
        adapter = _bigquery_adapter(StubCursor(rows), "seedbank")

        try:
            listed = {t.fqn: t.type for t in adapter.list_tables(include=["*"], exclude=[])}
            ddl = adapter.extract_ddl("seedbank.accession_copy")
        finally:
            adapter.close()

        assert listed == {"seedbank.accession": "table", "seedbank.accession_copy": "table"}
        assert ddl.startswith("CREATE TABLE seedbank.accession_copy CLONE x;")

    def test_an_external_table_and_a_snapshot_are_tables_profiled_only_on_opt_in(self) -> None:
        rows: list[tuple[object, ...]] = [
            ("accession", "BASE TABLE", "CREATE TABLE seedbank.accession (a INT64);"),
            ("field_scan", "EXTERNAL", "CREATE EXTERNAL TABLE seedbank.field_scan OPTIONS();"),
            ("accession_then", "SNAPSHOT", "CREATE SNAPSHOT TABLE seedbank.accession_then;"),
        ]
        adapter = _bigquery_adapter(StubCursor(rows), "seedbank")

        try:
            listed = {
                t.fqn: (t.type, t.external, t.opt_in_only)
                for t in adapter.list_tables(include=["*"], exclude=[])
            }
            ddl = adapter.extract_ddl("seedbank.field_scan")
        finally:
            adapter.close()

        assert listed == {
            "seedbank.accession": ("table", False, False),
            "seedbank.field_scan": ("table", True, False),
            "seedbank.accession_then": ("table", False, True),
        }
        assert ddl.startswith("CREATE EXTERNAL TABLE seedbank.field_scan")

    @pytest.mark.parametrize(("table_type", "refused"), [("EXTERNAL", False), ("BASE TABLE", True)])
    def test_a_refused_estimate_is_no_estimate_only_for_an_external_table(
        self,
        table_type: str,
        refused: bool,
    ) -> None:
        adapter = _bigquery_adapter(
            _RefusingPartitions([("field_scan", table_type, None)]),
            "seedbank",
        )

        try:
            adapter.list_tables(include=["*"], exclude=[])

            if refused:
                with pytest.raises(QueryFailed):
                    adapter.estimate_row_count("seedbank.field_scan")
            else:
                assert adapter.estimate_row_count("seedbank.field_scan") is None
        finally:
            adapter.close()

    def test_an_excluded_external_table_is_not_listed(self) -> None:
        rows: list[tuple[object, ...]] = [("field_scan", "EXTERNAL", None)]
        adapter = _bigquery_adapter(StubCursor(rows), "seedbank")

        try:
            listed = adapter.list_tables(include=["*"], exclude=["seedbank.field_scan"])
        finally:
            adapter.close()

        assert listed == []


def _table(adapter: BigqueryAdapter, dataset: str, name: str):
    return next(
        t for t in adapter.list_tables(include=["*"], exclude=[]) if t.fqn.split(".")[-1] == name
    )


class TestArrayAndStructColumnsDoNotFailTheTable:
    """BigQuery's parametrized spellings (`ARRAY<STRING>`, `STRUCT<a INT64>`) must classify as
    unsupported, or Phase A runs its string-like branch and BigQuery rejects the whole statement.
    """

    def test_an_array_column_is_unsupported_and_its_siblings_still_profile(
        self,
        bigquery_test_dataset,
    ) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.array_col (tags ARRAY<STRING>, n INT64)")
        cursor.execute(
            f"INSERT INTO `{dataset}`.array_col (tags, n) VALUES (['a', 'b'], 1), (['c'], 2)",
        )
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            table = _table(adapter, dataset, "array_col")
            columns = adapter.introspect_columns(table.fqn)
            counts, phase_a = adapter.compute_base_statistics(
                table.fqn,
                columns,
                StatisticsConfig(),
            )
            base = phase_a.stats
        finally:
            adapter.close()

        assert counts.row_count == 2
        assert base["tags"].supported is False
        assert base["n"].supported is True
        assert base["n"].cardinality == 2

    def test_a_struct_column_is_unsupported_and_its_siblings_still_profile(
        self,
        bigquery_test_dataset,
    ) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.struct_col (info STRUCT<a INT64>, n INT64)")
        cursor.execute(
            f"INSERT INTO `{dataset}`.struct_col (info, n) VALUES (STRUCT(1), 1), (STRUCT(2), 2)",
        )
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            table = _table(adapter, dataset, "struct_col")
            columns = adapter.introspect_columns(table.fqn)
            counts, phase_a = adapter.compute_base_statistics(
                table.fqn,
                columns,
                StatisticsConfig(),
            )
            base = phase_a.stats
        finally:
            adapter.close()

        assert counts.row_count == 2
        assert base["info"].supported is False
        assert base["n"].supported is True


class TestGeographyAndJsonColumnsDoNotFailTheTable:
    """GEOGRAPHY and JSON both refuse `APPROX_COUNT_DISTINCT` directly - the BigQuery
    documentation groups neither - so a call outside the type branches fails Phase A entirely.
    """

    def test_a_geography_column_is_kept_out_of_the_count_and_its_siblings_still_profile(
        self,
        bigquery_test_dataset,
    ) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.geo_col (pt GEOGRAPHY, n INT64)")
        cursor.execute(
            f"INSERT INTO `{dataset}`.geo_col (pt, n) VALUES "
            "(ST_GEOGPOINT(1, 1), 1), (ST_GEOGPOINT(2, 2), 2)",
        )
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            table = _table(adapter, dataset, "geo_col")
            columns = adapter.introspect_columns(table.fqn)
            counts, phase_a = adapter.compute_base_statistics(
                table.fqn,
                columns,
                StatisticsConfig(),
            )
            base = phase_a.stats
        finally:
            adapter.close()

        assert counts.row_count == 2
        # Spatial: null-counted alone, described by its own read rather than a distinct count.
        assert base["pt"].supported is True
        assert base["pt"].cardinality == 0
        assert base["n"].cardinality == 2

    def test_a_json_column_still_measures_a_real_cardinality(
        self,
        bigquery_test_dataset,
    ) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.json_col (payload JSON, n INT64)")
        cursor.execute(
            f"INSERT INTO `{dataset}`.json_col (payload, n) VALUES "
            "(JSON '{\"a\": 1}', 1), (JSON '{\"a\": 1}', 2), (JSON '{\"a\": 2}', 3)",
        )
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            table = _table(adapter, dataset, "json_col")
            columns = adapter.introspect_columns(table.fqn)
            counts, phase_a = adapter.compute_base_statistics(
                table.fqn,
                columns,
                StatisticsConfig(),
            )
            base = phase_a.stats
        finally:
            adapter.close()

        assert counts.row_count == 3
        # A real measurement (2 distinct encodings), not the crash the raw JSON argument caused.
        assert base["payload"].cardinality == 2


class TestNearUniqueColumnsAreRecountedExactly:
    """SPEC 2.2.2: `APPROX_COUNT_DISTINCT`'s own imprecision can cost a genuine primary key its
    SPEC 4.2 `candidate_key` verdict - a near-unique column is re-counted with `COUNT(DISTINCT)`.
    """

    def test_a_genuinely_unique_column_settles_to_an_exact_cardinality_method(
        self,
        bigquery_test_dataset,
    ) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.near_unique (id INT64)")
        values = ", ".join(f"({i})" for i in range(200))
        cursor.execute(f"INSERT INTO `{dataset}`.near_unique (id) VALUES {values}")
        adapter = _bigquery_adapter(cursor, dataset)

        try:
            table = _table(adapter, dataset, "near_unique")
            columns = adapter.introspect_columns(table.fqn)
            _counts, phase_a = adapter.compute_base_statistics(
                table.fqn,
                columns,
                StatisticsConfig(),
            )
            base = phase_a.stats
        finally:
            adapter.close()

        assert base["id"].cardinality == 200
        assert base["id"].cardinality_method == "exact"


class TestTemporalValueListFailureDegradesTheWholeBlock:
    """`distribution` and `frequencies` are REQUIRED on a temporal column (SPEC 2.2.3), so a failed
    value-list query degrades the whole temporal block rather than classifying an empty count list.
    """

    def test_a_failed_value_list_query_publishes_no_fabricated_verdict(
        self,
        bigquery_test_dataset,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cursor, dataset = bigquery_test_dataset
        cursor.execute(f"CREATE TABLE `{dataset}`.temporal_fail (t TIMESTAMP)")
        # More distinct values than the default enumeration_threshold (50), so the column
        # classifies `temporal` rather than falling under it into `categorical`.
        rows = ", ".join(
            f"(TIMESTAMP_ADD(TIMESTAMP '2024-01-01', INTERVAL {i} DAY))" for i in range(60)
        )
        cursor.execute(f"INSERT INTO `{dataset}`.temporal_fail (t) VALUES {rows}")
        adapter = _bigquery_adapter(cursor, dataset)

        def _always_fails(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("Resources exceeded during query execution")

        monkeypatch.setattr(
            "dbprint.adapters.bigquery.stats._approximate_distribution_via_top_n",
            _always_fails,
        )

        try:
            table = _table(adapter, dataset, "temporal_fail")
            columns = adapter.introspect_columns(table.fqn)
            counts, phase_a = adapter.compute_base_statistics(
                table.fqn,
                columns,
                StatisticsConfig(),
            )
            base = phase_a.stats
            enriched = adapter.compute_column_statistics(
                table.fqn,
                columns,
                StatisticsConfig(),
                counts,
                base,
                frozenset(),
            )
        finally:
            adapter.close()

        stat = enriched["t"]
        assert stat.distribution is None, (
            "a distribution classified from zero observations reads `uniform` - a measured "
            "verdict over nothing measured"
        )
        assert stat.frequencies is None, "frequencies of {0,0,0,0} claim a top-N that never ran"
        assert stat.values is None, "an empty list states a measured domain; absence states none"
        assert stat.range is not None and stat.percentiles, (
            "only the top-N statement is guarded, so the fused scalar row above it survived - "
            "discarding what was measured would make the marker beside it an over-claim"
        )
        # SPEC 2.2.4: exactly the three the failed statement produced, each absent - a name the
        # column also emits and a name its classification never required are both errors.
        assert stat.unmeasured == ("distribution", "frequencies", "values")
        assert all(getattr(stat, name) is None for name in stat.unmeasured)


class _RefusingPartitions(StubCursor):
    def execute(self, sql: str, params: object = None) -> StubCursor:
        if "PARTITIONS" in sql:
            raise RuntimeError("Not found: PARTITIONS has no storage statistics for this table")

        return super().execute(sql, params)
