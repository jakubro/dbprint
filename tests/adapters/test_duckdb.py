"""DuckdbAdapter-specific tests - behavior the generic contract suite does not cover."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest

from dbprint.adapters import DuckdbAdapter, TableScope


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect(":memory:")
    connection.execute("CREATE SCHEMA seedbank")
    connection.execute(
        """
        CREATE TABLE seedbank.herbarium (
            id UUID PRIMARY KEY,
            name VARCHAR NOT NULL,
            code VARCHAR
        )
        """,
    )
    connection.execute("CREATE UNIQUE INDEX herbarium_code_ux ON seedbank.herbarium (code)")
    connection.execute("CREATE INDEX herbarium_name_idx ON seedbank.herbarium (name)")
    connection.execute("COMMENT ON TABLE seedbank.herbarium IS 'a herbarium'")
    connection.execute(
        "CREATE TABLE seedbank.curator (id UUID PRIMARY KEY, "
        "herbarium_id UUID REFERENCES seedbank.herbarium(id))",
    )
    connection.execute("CREATE VIEW seedbank.active_curators_v AS SELECT id FROM seedbank.curator")

    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def adapter(con: duckdb.DuckDBPyConnection) -> Iterator[DuckdbAdapter]:
    a = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _params: con)
    a.connect()

    try:
        yield a
    finally:
        a.close()


class TestDdlFromCatalog:
    """duckdb carries its own `CREATE` statement in the catalog - no shell-out, no GET_DDL."""

    def test_a_table_ddl_is_its_own_create_statement(self, adapter: DuckdbAdapter) -> None:
        ddl = adapter.extract_ddl("memory.seedbank.herbarium")

        assert ddl.startswith("CREATE TABLE")
        assert "herbarium" in ddl.lower()

    def test_a_view_ddl_is_its_own_create_statement(self, adapter: DuckdbAdapter) -> None:
        ddl = adapter.extract_ddl("memory.seedbank.active_curators_v")

        assert ddl.startswith("CREATE VIEW")

    def test_an_unknown_table_raises(self, adapter: DuckdbAdapter) -> None:
        with pytest.raises(ValueError, match="no DDL available"):
            adapter.extract_ddl("memory.seedbank.does_not_exist")


class TestBareUniqueIndexPlacement:
    """A bare `CREATE UNIQUE INDEX` is declared-unique (SPEC 2.6.7), not a secondary index."""

    def test_the_bare_unique_index_is_a_unique_key_not_an_index(
        self,
        adapter: DuckdbAdapter,
    ) -> None:
        unique_keys = adapter.introspect_unique_keys("memory.seedbank.herbarium")
        indexes = adapter.introspect_indexes("memory.seedbank.herbarium")

        assert any(k.columns == ("code",) and not k.primary for k in unique_keys)
        assert not any(i.columns == ("code",) for i in indexes)

    def test_the_plain_index_stays_a_secondary_index(self, adapter: DuckdbAdapter) -> None:
        indexes = adapter.introspect_indexes("memory.seedbank.herbarium")

        assert any(i.columns == ("name",) and not i.unique for i in indexes)

    def test_the_primary_key_is_a_unique_key_marked_primary(self, adapter: DuckdbAdapter) -> None:
        unique_keys = adapter.introspect_unique_keys("memory.seedbank.herbarium")

        assert any(k.columns == ("id",) and k.primary for k in unique_keys)


class TestForeignKeyActionsAreAlwaysNoAction:
    """duckdb's parser rejects CASCADE/SET NULL/SET DEFAULT on a foreign key outright."""

    def test_a_declared_fk_reports_no_action_both_ways(self, adapter: DuckdbAdapter) -> None:
        fks = adapter.introspect_relationships("memory.seedbank.curator")
        fk = next(f for f in fks if f.column == ("herbarium_id",))

        assert fk.on_delete == "NO ACTION"
        assert fk.on_update == "NO ACTION"
        assert fk.target_table == "memory.seedbank.herbarium"


class TestNoPhysicalLayout:
    """duckdb has no declarative clustering/partitioning key for an ordinary table."""

    def test_physical_layout_is_always_none(self, adapter: DuckdbAdapter) -> None:
        assert adapter.introspect_physical_layout("memory.seedbank.herbarium") is None


class TestNoViewDependencies:
    """`duckdb_dependencies()` misses a plain view's read of a table entirely - no exact
    source, so the adapter omits the key unconditionally rather than publish a guess.
    """

    def test_view_dependencies_is_always_none(self, adapter: DuckdbAdapter) -> None:
        assert adapter.introspect_view_dependencies() is None


class TestCommentsFromCatalog:
    def test_table_comment_reaches_the_artifact(self, adapter: DuckdbAdapter) -> None:
        comments = adapter.extract_comments("memory.seedbank.herbarium")

        assert comments.table == "a herbarium"


class TestViewRowCountEstimate:
    """`duckdb_views()` carries no `estimated_size` column - a view is never queried
    (SPEC 2.2.15), so this is the adapter's own confirmation, not just the fixture's.
    """

    def test_a_view_carries_no_row_count_estimate(self, adapter: DuckdbAdapter) -> None:
        assert adapter.estimate_row_count("memory.seedbank.active_curators_v") is None


class TestSeededDrawReproduces:
    """A seeded fixed-size draw that reproduces exactly - the capability Snowflake's substrate
    cannot offer, measured against this adapter rather than assumed from the engine.
    """

    def test_a_fraction_scope_draws_the_same_values_across_two_calls(
        self,
        con: duckdb.DuckDBPyConnection,
    ) -> None:
        con.execute("CREATE TABLE seedbank.wide AS SELECT range AS id FROM range(2000)")
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _p: con)
        adapter.connect()
        scope = TableScope(sample=0.1)

        first = adapter.sample_values("memory.seedbank.wide", "id", 10, scope)
        second = adapter.sample_values("memory.seedbank.wide", "id", 10, scope)
        adapter.close()

        assert first == second

    def test_a_fixed_size_scope_draws_the_same_values_across_two_calls(
        self,
        con: duckdb.DuckDBPyConnection,
    ) -> None:
        """The oversample step itself is seeded here, unlike Snowflake's own unseedable draw."""

        con.execute("CREATE TABLE seedbank.wide AS SELECT range AS id FROM range(2000)")
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _p: con)
        adapter.connect()

        first = adapter.sample_values("memory.seedbank.wide", "id", 10)
        second = adapter.sample_values("memory.seedbank.wide", "id", 10)
        adapter.close()

        assert first == second


class TestNoSessionSettingIsForced:
    """The seeded draw's reproducibility costs the user nothing - unlike the substrate's own
    `SET threads = 1`, the adapter changes no session setting they did not ask for.
    """

    def test_a_sampled_run_issues_no_set_statement(
        self,
        con: duckdb.DuckDBPyConnection,
    ) -> None:
        from tests.adapters.test_dialect_guard import _install_recorder

        con.execute("CREATE TABLE seedbank.wide AS SELECT range AS id FROM range(2000)")
        adapter = DuckdbAdapter({"database": ":memory:"}, cursor_factory=lambda _p: con)
        adapter.connect()
        recorder = _install_recorder(adapter)

        adapter.sample_values("memory.seedbank.wide", "id", 10, TableScope(sample=0.1))
        adapter.close()

        assert not any(s.strip().lower().startswith("set ") for s in recorder.statements)
