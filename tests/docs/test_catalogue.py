"""`catalogue.py` - reading connections, tables, and artifacts off disk."""

from __future__ import annotations

from pathlib import Path

from dbprint.config import ConnectionConfig
from dbprint.docs import catalogue


class TestLoadConnections:
    def test_loads_every_connection_with_a_readable_manifest(
        self,
        rich_conn: ConnectionConfig,
    ) -> None:
        loaded = catalogue.load_connections([rich_conn])

        assert [c.name for c in loaded] == ["primary"]
        assert set(loaded[0].tables) == {"seedbank.batch", "seedbank.cultivar"}

    def test_drops_a_connection_with_no_manifest(self, tmp_path: Path) -> None:
        conn = ConnectionConfig(name="empty", adapter="postgres", output=tmp_path / "prints")

        assert catalogue.load_connections([conn]) == []


class TestFindConnection:
    def test_finds_by_name(self, rich_conn: ConnectionConfig) -> None:
        loaded = catalogue.load_connections([rich_conn])

        found = catalogue.find_connection(loaded, "primary")

        assert found is not None
        assert found.name == "primary"

    def test_returns_none_for_unknown_name(self, rich_conn: ConnectionConfig) -> None:
        loaded = catalogue.load_connections([rich_conn])

        assert catalogue.find_connection(loaded, "nope") is None


class TestLoadTable:
    def test_reads_every_declared_artifact(self, rich_conn: ConnectionConfig) -> None:
        conn = catalogue.load_connections([rich_conn])[0]

        artifacts = catalogue.load_table(conn, "seedbank.batch")

        assert artifacts is not None
        assert artifacts.statistics is not None
        assert artifacts.relationships is not None
        assert artifacts.statistics_annotations is not None
        assert artifacts.relationships_annotations is not None
        assert "One row per seed batch" in (artifacts.description or "")
        assert "CREATE TABLE" in (artifacts.ddl or "")

    def test_undeclared_artifact_is_none_not_an_error(self, rich_conn: ConnectionConfig) -> None:
        conn = catalogue.load_connections([rich_conn])[0]

        artifacts = catalogue.load_table(conn, "seedbank.cultivar")

        assert artifacts is not None
        assert artifacts.description is None
        assert artifacts.statistics_annotations is None

    def test_unknown_table_is_none(self, rich_conn: ConnectionConfig) -> None:
        conn = catalogue.load_connections([rich_conn])[0]

        assert catalogue.load_table(conn, "seedbank.nonexistent") is None


class TestLoadRelationships:
    def test_reads_relationships_alone(self, rich_conn: ConnectionConfig) -> None:
        conn = catalogue.load_connections([rich_conn])[0]

        relationships = catalogue.load_relationships(conn, "seedbank.batch")

        assert relationships is not None
        assert relationships["refers_to"][0]["target_table"] == "seedbank.cultivar"


class TestSchemaKey:
    def test_multi_part_name(self) -> None:
        assert catalogue.schema_key("seedbank.batch") == "seedbank"

    def test_three_part_name_keeps_every_segment_but_the_leaf(self) -> None:
        assert catalogue.schema_key("db.schema.table") == "db.schema"

    def test_bare_name_has_no_schema(self) -> None:
        assert catalogue.schema_key("orphan") == "(none)"


class TestTablesInSchema:
    def test_filters_to_one_schema(self, rich_conn: ConnectionConfig) -> None:
        conn = catalogue.load_connections([rich_conn])[0]

        tables = catalogue.tables_in_schema(conn, "seedbank")

        assert set(tables) == {"seedbank.batch", "seedbank.cultivar"}


class TestPrefixTree:
    def test_groups_by_shared_prefix(self) -> None:
        tree = catalogue.prefix_tree(["a.x", "a.y", "b.z"])

        assert tree.leaves == ()
        assert set(tree.groups) == {"a", "b"}
        assert tree.groups["a"].leaves == ("a.x", "a.y")

    def test_bare_names_are_leaves_at_the_root(self) -> None:
        tree = catalogue.prefix_tree(["orphan", "a.x"])

        assert tree.leaves == ("orphan",)


class TestLeafTargets:
    def test_maps_leaf_name_to_table_url(self, rich_conn: ConnectionConfig) -> None:
        conn = catalogue.load_connections([rich_conn])[0]

        targets = catalogue.leaf_targets(conn, "seedbank.batch")

        assert targets["cultivar"] == "/t/primary/seedbank.cultivar"

    def test_same_schema_wins_a_leaf_collision(self, tmp_path: Path) -> None:
        conn = catalogue.PrintConnection(
            name="c",
            root=tmp_path,
            manifest={},
            tables={"a.collector": {}, "b.collector": {}},
        )

        targets = catalogue.leaf_targets(conn, "a.collector")

        assert targets["collector"] == "/t/c/a.collector"
