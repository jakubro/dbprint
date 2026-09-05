"""Databricks-specific behaviors the contract suite's default fixtures do not exercise.

The local PySpark + Delta fixtures cover the `information_schema`-absent fallback only; Unity
Catalog's path runs on `RecordedResponseCursor`'s rows, never against a real engine.
"""

from __future__ import annotations

import json

import pytest

from dbprint.adapters import DatabricksAdapter, StatisticsConfig
from dbprint.adapters.databricks.introspect import UnmappedTableType
from dbprint.spec.sketch import low64_md5
from tests.adapters.conftest import RecordedResponseCursor


_CREDS = {
    "server_hostname": "local",
    "http_path": "local",
    "access_token": "local",
    "catalog": "spark_catalog",
}

_UC_CREDS = {
    "server_hostname": "local",
    "http_path": "local",
    "access_token": "local",
    "catalog": "main",
}


def _databricks_adapter(cursor) -> DatabricksAdapter:
    adapter = DatabricksAdapter(_CREDS, cursor_factory=lambda _params: cursor)
    adapter.connect()

    return adapter


def _uc_adapter(responses: dict[str, list[tuple]]) -> DatabricksAdapter:
    cursor = RecordedResponseCursor(responses)
    adapter = DatabricksAdapter(_UC_CREDS, cursor_factory=lambda _params: cursor)
    adapter.connect()

    return adapter


class TestTheMetastoreFoldsNamesItself:
    """Why this adapter carries no physical spelling: neither branch can hold a mixed-case name.

    Unity Catalog is documented rather than tested here; the legacy metastore folds on creation.
    """

    def test_a_mixed_case_table_is_stored_folded(self, databricks_test_schema) -> None:
        databricks_test_schema.execute("CREATE TABLE MixedCase (id INT) USING DELTA")
        databricks_test_schema.execute("SHOW TABLES")
        names = {row[1] for row in databricks_test_schema.fetchall()}

        assert "mixedcase" in names
        assert "MixedCase" not in names

    def test_the_folded_name_addresses_it(self, databricks_test_schema) -> None:
        databricks_test_schema.execute("CREATE TABLE MixedRead (id INT) USING DELTA")
        databricks_test_schema.execute("INSERT INTO MixedRead VALUES (1), (2)")
        adapter = _databricks_adapter(databricks_test_schema)

        try:
            fqn = next(
                t.fqn
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.endswith(".mixedread")
            )
            cols = adapter.introspect_columns(fqn)
            counts, _base = adapter.compute_base_statistics(fqn, cols, StatisticsConfig())
        finally:
            adapter.close()

        assert fqn.endswith(".mixedread")
        assert [c.name for c in cols] == ["id"]
        assert counts.row_count == 2


class TestUnityCatalogDetection:
    def test_the_local_substrate_is_detected_as_the_fallback(self, databricks_test_schema) -> None:
        """No `information_schema` locally, so the probe fails and `connect()` records that."""

        adapter = _databricks_adapter(databricks_test_schema)

        try:
            assert adapter._unity_catalog is False
        finally:
            adapter.close()


class TestFallbackColumnsCarryNoNullability:
    def test_a_not_null_column_is_still_reported_nullable(self, databricks_test_schema) -> None:
        """`DESCRIBE TABLE` carries no nullability field at all on the fallback path - reporting
        `True` unconditionally is the honest answer, not a guess dressed as a measurement.
        """

        databricks_test_schema.execute(
            "CREATE TABLE strict_cols (a INT NOT NULL, b INT) USING DELTA",
        )
        adapter = _databricks_adapter(databricks_test_schema)

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "strict_cols"
            )
            columns = adapter.introspect_columns(table.fqn)
        finally:
            adapter.close()

        assert {c.name: c.nullable for c in columns} == {"a": True, "b": True}


class TestPhysicalLayout:
    """`DESCRIBE DETAIL`'s clusteringColumns/partitionColumns, see ARCHITECTURE.md 2."""

    def test_a_cluster_by_table_reports_cluster(self, databricks_test_schema) -> None:
        databricks_test_schema.execute(
            "CREATE TABLE clustered (a INT, b INT) USING DELTA CLUSTER BY (a, b)",
        )
        adapter = _databricks_adapter(databricks_test_schema)

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "clustered"
            )
            layout = adapter.introspect_physical_layout(table.fqn)
        finally:
            adapter.close()

        assert layout is not None
        assert layout.mechanism == "cluster"
        assert [k.column for k in layout.keys] == ["a", "b"]

    def test_a_partitioned_table_reports_partition(self, databricks_test_schema) -> None:
        databricks_test_schema.execute(
            "CREATE TABLE partitioned (a INT, b INT) USING DELTA PARTITIONED BY (a)",
        )
        adapter = _databricks_adapter(databricks_test_schema)

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "partitioned"
            )
            layout = adapter.introspect_physical_layout(table.fqn)
        finally:
            adapter.close()

        assert layout is not None
        assert layout.mechanism == "partition"
        assert [k.column for k in layout.keys] == ["a"]

    def test_a_plain_table_is_absent_not_empty(self, databricks_test_schema) -> None:
        databricks_test_schema.execute("CREATE TABLE plain_layout (a INT) USING DELTA")
        adapter = _databricks_adapter(databricks_test_schema)

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "plain_layout"
            )
            layout = adapter.introspect_physical_layout(table.fqn)
        finally:
            adapter.close()

        assert layout is None


class TestKeySketch:
    """SPEC 2.2.14's low64_md5, ported through `CONV`'s hex-to-decimal reading."""

    def test_agrees_with_the_shared_spec_definition_on_a_wide_sample(
        self,
        databricks_test_schema,
    ) -> None:
        """Every adapter's in-database hash MUST reproduce `spec.sketch.low64_md5` exactly."""

        databricks_test_schema.execute("CREATE TABLE hashed (v STRING) USING DELTA")
        values = [f"value-{i}" for i in range(200)]
        rows = ", ".join(f"('{v}')" for v in values)
        databricks_test_schema.execute(f"INSERT INTO hashed (v) VALUES {rows}")

        adapter = _databricks_adapter(databricks_test_schema)

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "hashed"
            )
            sketch = adapter.compute_key_sketch(table.fqn, "v", "string", "text", k=1000)
        finally:
            adapter.close()

        expected = sorted(low64_md5(v) for v in values)

        assert list(sketch) == expected

    def test_every_value_is_a_full_unsigned_64_bit_integer(self, databricks_test_schema) -> None:
        """A value whose top bit is set must not sort negative or overflow `CONV`'s range."""

        databricks_test_schema.execute("CREATE TABLE hashed2 (v STRING) USING DELTA")
        values = [f"probe-{i}" for i in range(500)]
        rows = ", ".join(f"('{v}')" for v in values)
        databricks_test_schema.execute(f"INSERT INTO hashed2 (v) VALUES {rows}")

        adapter = _databricks_adapter(databricks_test_schema)

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "hashed2"
            )
            sketch = adapter.compute_key_sketch(table.fqn, "v", "string", "text", k=1000)
        finally:
            adapter.close()

        assert sketch, "the probe table seeded no distinct values"
        assert all(0 <= h < 2**64 for h in sketch)
        # 500 draws make a top-bit-set value near-certain - a sketch built only from small
        # values would still pass a regression to the wrong MD5 half or a dropped high bit.
        assert any(h >= 2**63 for h in sketch), "no sampled hash exercised the top bit"


class TestDefaultCollation:
    def test_reports_the_engine_default_with_no_session_override(
        self,
        databricks_test_schema,
    ) -> None:
        adapter = _databricks_adapter(databricks_test_schema)

        try:
            collation = adapter.default_collation()
        finally:
            adapter.close()

        assert collation == "UTF8_BINARY"


class TestTemporaryViewCannotRetypeARealTable:
    """`SHOW VIEWS IN <schema>` also lists session-local temporary views regardless of the schema
    named, so a same-named temp view must not retype the real table as a view.
    """

    def test_a_same_named_temp_view_does_not_retype_the_real_table(
        self,
        databricks_test_schema,
    ) -> None:
        databricks_test_schema.execute("CREATE TABLE batch (a INT) USING DELTA")
        databricks_test_schema.execute("CREATE TEMPORARY VIEW batch AS SELECT 1 AS a")
        adapter = _databricks_adapter(databricks_test_schema)

        try:
            tables = {
                t.fqn.split(".")[-1]: t.type for t in adapter.list_tables(include=["*"], exclude=[])
            }
        finally:
            adapter.close()

        assert tables["batch"] == "table", (
            f"the real table was retyped by a same-named session-local temp view: {tables}"
        )


class TestPhysicalLayoutAndCommentsFallBackForAView:
    """`DESCRIBE DETAIL` refuses a view outright, so `DESCRIBE TABLE EXTENDED`'s detailed-info
    block is the only source that answers a documented view's comment.
    """

    def test_a_views_comment_is_still_read(self, databricks_test_schema) -> None:
        databricks_test_schema.execute("CREATE TABLE curator_profile (a INT) USING DELTA")
        databricks_test_schema.execute(
            "CREATE VIEW active_curators_v COMMENT 'a view comment' "
            "AS SELECT a FROM curator_profile",
        )
        adapter = _databricks_adapter(databricks_test_schema)

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn.split(".")[-1] == "active_curators_v"
            )
            comments = adapter.extract_comments(table.fqn)
            layout = adapter.introspect_physical_layout(table.fqn)
        finally:
            adapter.close()

        assert comments.table == "a view comment"
        # A view has no physical layout at all - DESCRIBE DETAIL's own refusal correctly
        # degrades to "none declared", the fallback carrying no clustering concept either.
        assert layout is None


class TestUnmappedTableType:
    """Databricks documents exactly eight `table_type` values - a ninth, unrecognised one must
    surface loudly rather than silently vanish the object it names.
    """

    def test_an_unrecognised_table_type_raises(self) -> None:
        responses = {
            "tables": [("garden", "mystery_object", "SOME_FUTURE_TYPE")],
        }
        adapter = _uc_adapter(responses)

        try:
            with pytest.raises(UnmappedTableType, match="SOME_FUTURE_TYPE"):
                adapter.list_tables(include=["*"], exclude=[])
        finally:
            adapter.close()

    def test_every_documented_table_type_is_mapped(self) -> None:
        documented = [
            "MANAGED",
            "EXTERNAL",
            "VIEW",
            "FOREIGN",
            "STREAMING_TABLE",
            "MATERIALIZED_VIEW",
            "MANAGED_SHALLOW_CLONE",
            "EXTERNAL_SHALLOW_CLONE",
        ]
        responses = {
            "tables": [("garden", f"t_{i}", t) for i, t in enumerate(documented)],
        }
        adapter = _uc_adapter(responses)

        try:
            tables = {
                t.fqn.split(".")[-1]: t.type for t in adapter.list_tables(include=["*"], exclude=[])
            }
        finally:
            adapter.close()

        assert tables == {
            "t_0": "table",
            "t_1": "table",
            "t_2": "view",
            "t_3": "table",
            "t_4": "table",
            "t_5": "matview",
            "t_6": "table",
            "t_7": "table",
        }


class TestUnityCatalogColumns:
    """`data_type` is only the simple type name and `full_data_type` carries precision/scale;
    `DESCRIBE TABLE EXTENDED ... AS JSON` is the documented source for default and collation.
    """

    def test_full_data_type_default_and_collation_are_all_read(self) -> None:
        extended_json = json.dumps(
            {
                "collation": "UTF8_BINARY",
                "columns": [
                    {"name": "accession_id", "type": {"name": "int"}, "default": None},
                    {
                        "name": "viability_pct",
                        "type": {"name": "decimal", "precision": 18, "scale": 4},
                        "default": "0.00",
                    },
                    {
                        "name": "curator_name",
                        "type": {"name": "string", "collation": "UNICODE_CI"},
                        "default": None,
                    },
                ],
            },
        )
        responses = {
            "columns": [
                ("accession_id", 1, "INT", "NO"),
                ("viability_pct", 2, "DECIMAL(18,4)", "YES"),
                ("curator_name", 3, "STRING", "YES"),
            ],
            "describe_extended_json": [(extended_json,)],
        }
        adapter = _uc_adapter(responses)

        try:
            columns = {c.name: c for c in adapter.introspect_columns("garden.accession")}
        finally:
            adapter.close()

        assert columns["viability_pct"].sql_type == "DECIMAL(18,4)", (
            "a widening DECIMAL(10,2) -> DECIMAL(18,4) would diff as unchanged under the "
            "simple type name alone"
        )
        assert columns["viability_pct"].default == "0.00"
        assert columns["accession_id"].default is None
        assert columns["curator_name"].collation == "UNICODE_CI"
        assert columns["accession_id"].collation is None, (
            "a column matching the table's own default collation must not carry an override"
        )


class TestCompositeForeignKeyPairing:
    """`position_in_unique_constraint` names which ordinal of the parent key each referencing
    column maps to - the parent's columns may be listed in any order, so zipping mispairs them.
    """

    def test_a_reordered_composite_key_pairs_correctly(self) -> None:
        # Child FK (fk2, fk1) REFERENCES curator(pk2, pk1) while the parent PRIMARY KEY is
        # (pk1, pk2), so `position_in_unique_constraint` is the only correct way to pair them.
        responses = {
            "key_column_usage_fk": [
                ("fk_curator", "fk2", 2, "garden", "seedbank", "pk_curator"),
                ("fk_curator", "fk1", 1, "garden", "seedbank", "pk_curator"),
            ],
            "key_column_usage": [
                ("curator", 1, "pk1"),
                ("curator", 2, "pk2"),
            ],
        }
        adapter = _uc_adapter(responses)

        try:
            edges = adapter.introspect_relationships("seedbank.accession")
        finally:
            adapter.close()

        assert len(edges) == 1
        edge = edges[0]
        assert edge.column == ("fk2", "fk1"), "child columns must stay in their declared order"
        assert edge.target_column == ("pk2", "pk1"), (
            f"positional zipping would have paired (pk1, pk2) instead: got {edge.target_column}"
        )
        assert edge.target_table == "seedbank.curator"

    def test_a_cross_catalog_foreign_key_is_not_dropped(self) -> None:
        responses = {
            "key_column_usage_fk": [
                ("fk_collector", "collector_id", 1, "arboretum", "seedbank", "pk_collector"),
            ],
            "key_column_usage": [
                ("collector", 1, "id"),
            ],
        }
        adapter = _uc_adapter(responses)

        try:
            edges = adapter.introspect_relationships("seedbank.accession")
        finally:
            adapter.close()

        assert len(edges) == 1, (
            "a cross-catalog FK resolved against current_catalog() unconditionally vanishes"
        )
        # `arboretum` is not the connected catalog, so the target is outside this print entirely -
        # a bare `seedbank.collector` would name a different object that may well exist.
        assert edges[0].target_table == "arboretum.seedbank.collector"
