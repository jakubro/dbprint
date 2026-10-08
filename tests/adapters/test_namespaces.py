"""A connection without a namespace key reads every namespace it can, each from its own catalog.

Each class builds two namespaces on a shared substrate and scopes to them with selectors.
"""

from __future__ import annotations

import logging
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import duckdb
import psycopg
import pytest
from psycopg import sql

from dbprint.adapters import (
    ClickhouseAdapter,
    DatabricksAdapter,
    MockAdapter,
    MockTable,
    MysqlAdapter,
    PostgresAdapter,
    RedshiftAdapter,
    SnowflakeAdapter,
)
from dbprint.adapters.base import SkippedNamespace, TableMeta
from dbprint.adapters.errors import QueryFailed
from dbprint.adapters.identifiers import IdentifierRejected, enforce_table_identifiers
from dbprint.adapters.postgres import introspect as postgres_introspect
from dbprint.adapters.redshift.connection import RedshiftConnectionError
from dbprint.adapters.snowflake import introspect as snowflake_introspect
from dbprint.config import ConnectionConfig
from dbprint.engine import Engine
from tests._prints import columns, mock_table
from tests.adapters.conftest import (
    RecordedResponseCursor,
    RedshiftDialectShim,
    SnowflakeDialectShim,
    _mysql_admin_exec,
    _mysql_exec_many,
)
from tests.conftest import MysqlCluster, PostgresCluster, pg_connect


class TestMysql:
    def test_every_database_is_listed_and_read_from_its_own(
        self,
        mysql_cluster: MysqlCluster,
    ) -> None:
        a, b = (f"ns_{secrets.token_hex(3)}" for _ in range(2))

        for name, table in ((a, "orchard"), (b, "grove")):
            _mysql_admin_exec(mysql_cluster.port, f"CREATE DATABASE `{name}`")
            _mysql_exec_many(mysql_cluster.port, name, [f"CREATE TABLE {table} (id INT, tag TEXT)"])

        adapter = MysqlAdapter(
            {"host": "127.0.0.1", "port": str(mysql_cluster.port), "user": "root", "password": ""},
        )
        adapter.connect()

        try:
            fqns = [t.fqn for t in adapter.list_tables(include=[f"{a}.*", f"{b}.*"], exclude=[])]
            columns = [c.name for c in adapter.introspect_columns(f"{b}.grove")]
        finally:
            adapter.close()

            for name in (a, b):
                _mysql_admin_exec(mysql_cluster.port, f"DROP DATABASE `{name}`")

        assert sorted(fqns) == sorted([f"{a}.orchard", f"{b}.grove"])
        assert columns == ["id", "tag"]

    def test_the_system_databases_are_never_listed(self, mysql_cluster: MysqlCluster) -> None:
        adapter = MysqlAdapter(
            {"host": "127.0.0.1", "port": str(mysql_cluster.port), "user": "root", "password": ""},
        )
        adapter.connect()

        system = ("mysql", "information_schema", "performance_schema", "sys")

        try:
            listed = adapter.list_tables(include=[f"{name}.*" for name in system], exclude=[])
        finally:
            adapter.close()

        assert listed == []


class TestPostgres:
    def test_each_database_is_read_on_its_own_session(
        self,
        postgres_cluster: PostgresCluster,
    ) -> None:
        a, b = (f"ns_{secrets.token_hex(3)}" for _ in range(2))
        admin = postgres_cluster

        with pg_connect(admin.creds()) as conn:
            for name in (a, b):
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

        for name, table in ((a, "orchard"), (b, "grove")):
            with pg_connect(admin.creds(name)) as conn:
                conn.execute(
                    sql.SQL("CREATE TABLE public.{} (id int, tag text)").format(
                        sql.Identifier(table),
                    ),
                )

        adapter = PostgresAdapter(
            {
                "host": "127.0.0.1",
                "port": str(postgres_cluster.port),
                "user": "postgres",
                "password": "",
            },
        )
        adapter.connect()

        try:
            fqns = [t.fqn for t in adapter.list_tables(include=[f"{a}.*", f"{b}.*"], exclude=[])]
            columns = [c.name for c in adapter.introspect_columns(f"{b}.public.grove")]
            ddl = adapter.extract_ddl(f"{a}.public.orchard")
        finally:
            adapter.close()

            with pg_connect(admin.creds()) as conn:
                for name in (a, b):
                    conn.execute(
                        sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)),
                    )

        assert sorted(fqns) == sorted([f"{a}.public.orchard", f"{b}.public.grove"])
        assert columns == ["id", "tag"]
        assert "orchard" in ddl


class TestPostgresFollowThrough:
    """The dependency read isolates each database and reads only the ones the listing selected."""

    @pytest.fixture
    def three(self, postgres_cluster: PostgresCluster) -> Iterator[tuple[str, ...]]:
        names = tuple(f"ns_{secrets.token_hex(3)}" for _ in range(3))

        with pg_connect(postgres_cluster.creds()) as conn:
            for name in names:
                conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))

        for name in names:
            with pg_connect(postgres_cluster.creds(name)) as conn:
                conn.execute("CREATE TABLE public.bed (id int)")
                conn.execute("CREATE VIEW public.bed_view AS SELECT id FROM public.bed")

        yield names

        with pg_connect(postgres_cluster.creds()) as conn:
            for name in names:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))

    def test_one_unreadable_database_costs_only_its_own_views(
        self,
        postgres_cluster: PostgresCluster,
        three: tuple[str, ...],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        failing = three[1]
        real = postgres_introspect.view_dependencies

        def refusing(conn: Any, database: str) -> dict[str, tuple[str, ...]]:
            if database == failing:
                raise QueryFailed(RuntimeError("permission denied for pg_depend"), "SELECT", None)

            return real(conn, database)

        monkeypatch.setattr(postgres_introspect, "view_dependencies", refusing)
        adapter = _postgres_adapter(postgres_cluster)
        adapter.connect()

        try:
            adapter.list_tables(include=[f"{name}.*" for name in three], exclude=[])
            dependencies = adapter.introspect_view_dependencies() or {}
            unread = [entry.name for entry in adapter.unread_dependency_namespaces()]
        finally:
            adapter.close()

        views = {fqn.split(".", 1)[0] for fqn in dependencies}
        assert views == {three[0], three[2]}
        assert unread == [failing]

    def test_a_narrow_selection_opens_no_session_to_the_other_databases(
        self,
        postgres_cluster: PostgresCluster,
        three: tuple[str, ...],
    ) -> None:
        adapter = _postgres_adapter(postgres_cluster)
        adapter.connect()

        try:
            adapter.list_tables(include=[f"{three[0]}.*"], exclude=[])
            adapter.introspect_view_dependencies()

            with pg_connect(postgres_cluster.creds()) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT datname FROM pg_stat_activity WHERE datname = ANY(%s)",
                    (list(three),),
                ).fetchall()
        finally:
            adapter.close()

        assert [name for (name,) in rows] == [three[0]]


class TestSnowflake:
    def test_every_database_is_listed_through_its_own_information_schema(self) -> None:
        con = duckdb.connect(":memory:")
        con.execute("ATTACH ':memory:' AS orchard")
        con.execute("CREATE TABLE memory.main.grove (id INTEGER, tag VARCHAR)")
        con.execute("CREATE TABLE orchard.main.tree (id INTEGER, height INTEGER)")
        shim = SnowflakeDialectShim(con)
        adapter = SnowflakeAdapter(
            {"account": "a", "user": "u", "password": "p", "warehouse": "w", "role": "r"},
            cursor_factory=lambda _params: shim,
        )
        adapter.connect()

        try:
            fqns = [
                t.fqn for t in adapter.list_tables(include=["memory.*", "orchard.*"], exclude=[])
            ]
            columns = [c.name for c in adapter.introspect_columns("orchard.main.tree")]
        finally:
            adapter.close()

        assert fqns == ["memory.main.grove", "orchard.main.tree"]
        assert columns == ["id", "height"]

    def test_a_configured_database_takes_the_catalogs_own_spelling(self) -> None:
        con = duckdb.connect(":memory:")
        con.execute("CREATE TABLE memory.main.grove (id INTEGER)")
        shim = SnowflakeDialectShim(con)
        adapter = SnowflakeAdapter(
            {
                "account": "a",
                "user": "u",
                "password": "p",
                "warehouse": "w",
                "role": "r",
                "database": "MEMORY",
            },
            cursor_factory=lambda _params: shim,
        )
        adapter.connect()

        try:
            fqns = [t.fqn for t in adapter.list_tables(include=["*"], exclude=[])]
        finally:
            adapter.close()

        assert fqns == ["memory.main.grove"]


class TestClickhouse:
    def test_every_database_is_listed(self, clickhouse_native_connection: Any) -> None:
        cur = clickhouse_native_connection
        a, b = (f"ns_{secrets.token_hex(3)}" for _ in range(2))

        for name, table in ((a, "orchard"), (b, "grove")):
            cur.execute(f"CREATE DATABASE {name}")
            cur.execute(f"CREATE TABLE {name}.{table} (id Int32) ENGINE = Memory")

        adapter = ClickhouseAdapter({"host": "chdb"}, cursor_factory=lambda _params: cur)
        adapter.connect()

        try:
            fqns = [t.fqn for t in adapter.list_tables(include=[f"{a}.*", f"{b}.*"], exclude=[])]
        finally:
            adapter.close()

        assert sorted(fqns) == sorted([f"{a}.orchard", f"{b}.grove"])


class TestDatabricksUnityCatalog:
    _RESPONSES: ClassVar[dict[str, Any]] = {
        "catalogs": [("garden",), ("hive_metastore",), ("locked",), ("orchard",), ("system",)],
        "unreadable_catalogs": ["locked"],
        "tables:garden": [("seedbank", "accession", "MANAGED")],
        "tables:orchard": [("stock", "tree", "MANAGED")],
    }

    def _adapter(self) -> DatabricksAdapter:
        cursor = RecordedResponseCursor(self._RESPONSES)
        adapter = DatabricksAdapter(
            {"server_hostname": "h", "http_path": "p", "access_token": "t"},
            cursor_factory=lambda _params: cursor,
        )
        adapter.connect()

        return adapter

    def test_every_readable_catalog_is_listed_three_part(self) -> None:
        adapter = self._adapter()

        try:
            fqns = [t.fqn for t in adapter.list_tables(include=["*"], exclude=[])]
        finally:
            adapter.close()

        assert fqns == ["garden.seedbank.accession", "orchard.stock.tree"]

    def test_an_unreadable_catalog_is_skipped_and_named(self) -> None:
        adapter = self._adapter()

        try:
            adapter.list_tables(include=["*"], exclude=[])
            skipped = [entry.name for entry in adapter.skipped_namespaces()]
        finally:
            adapter.close()

        assert skipped == ["locked"]


class TestBigqueryCaseCollision:
    def test_datasets_differing_by_case_collide_on_one_path(self) -> None:
        meta = TableMeta(fqn="seedbank.taxon", type="table", namespace_path=("seedbank", "taxon"))

        with pytest.raises(
            IdentifierRejected,
            match=re.escape("case-collides-with-Seedbank.taxon"),
        ):
            enforce_table_identifiers(
                [(meta, ("Seedbank", "taxon")), (meta, ("seedbank", "taxon"))],
            )


class TestRedshift:
    def test_each_database_is_read_on_its_own_session(
        self,
        postgres_cluster: PostgresCluster,
    ) -> None:
        stand_ins = _redshift_stand_ins(postgres_cluster, {"alpha": "orchard", "beta": "grove"})
        shims = {name: RedshiftDialectShim(conn, database=name) for name, conn in stand_ins}
        entry = _EntryShim(shims)
        opened: list[str] = []

        def factory(params: Any) -> Any:
            opened.append(params.database)

            return entry if params.database == "dev" else shims[params.database]

        adapter = RedshiftAdapter(
            {"host": "redshift", "user": "u", "password": "p"},
            cursor_factory=factory,
        )
        adapter.connect()

        try:
            fqns = [t.fqn for t in adapter.list_tables(include=["*"], exclude=[])]
            columns = [c.name for c in adapter.introspect_columns("beta.public.grove")]
        finally:
            adapter.close()

            for _name, conn in stand_ins:
                conn.close()

        assert fqns == ["alpha.public.orchard", "beta.public.grove"]
        assert columns == ["id", "tag"]
        assert opened == ["dev", "alpha", "beta"]


class TestSnowflakeFollowThrough:
    def test_one_unreadable_database_costs_only_its_own_views(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        con = duckdb.connect(":memory:")
        con.execute("ATTACH ':memory:' AS orchard")
        con.execute("CREATE TABLE memory.main.grove (id INTEGER)")
        con.execute("CREATE TABLE orchard.main.tree (id INTEGER)")
        real = snowflake_introspect.view_dependencies

        def refusing(cursor: Any, databases: Any) -> dict[str, tuple[str, ...]]:
            if "orchard" in databases:
                raise QueryFailed(RuntimeError("insufficient privileges"), "SELECT", None)

            return {**real(cursor, databases), "memory.main.v": ()}

        monkeypatch.setattr(snowflake_introspect, "view_dependencies", refusing)
        adapter = SnowflakeAdapter(
            {"account": "a", "user": "u", "password": "p", "warehouse": "w", "role": "r"},
            cursor_factory=lambda _params: SnowflakeDialectShim(con),
        )
        adapter.connect()

        try:
            adapter.list_tables(include=["memory.*", "orchard.*"], exclude=[])
            dependencies = adapter.introspect_view_dependencies() or {}
            unread = [entry.name for entry in adapter.unread_dependency_namespaces()]
        finally:
            adapter.close()

        assert "memory.main.v" in dependencies
        assert unread == ["orchard"]


class TestRedshiftConfiguredSpelling:
    def test_a_configured_database_takes_the_catalogs_own_spelling(
        self,
        postgres_cluster: PostgresCluster,
    ) -> None:
        stand_ins = _redshift_stand_ins(postgres_cluster, {"alpha": "orchard", "beta": "grove"})
        shims = {name: RedshiftDialectShim(conn, database=name) for name, conn in stand_ins}
        entry = _EntryShim(shims)
        adapter = RedshiftAdapter(
            {"host": "redshift", "user": "u", "password": "p", "database": "ALPHA"},
            cursor_factory=lambda params: shims.get(params.database, entry),
        )
        adapter.connect()

        try:
            fqns = [t.fqn for t in adapter.list_tables(include=["*"], exclude=[])]
        finally:
            adapter.close()

            for _name, conn in stand_ins:
                conn.close()

        assert fqns == ["alpha.public.orchard"]

    def test_an_absent_configured_database_still_fails_to_connect(self) -> None:
        def factory(params: Any) -> Any:
            raise RuntimeError(f'database "{params.database}" does not exist')

        adapter = RedshiftAdapter(
            {"host": "redshift", "user": "u", "password": "p", "database": "nosuch"},
            cursor_factory=factory,
        )

        with pytest.raises(RedshiftConnectionError, match="nosuch"):
            adapter.connect()


class TestTheEngineWarns:
    def test_a_skipped_namespace_is_named_once(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        conn = ConnectionConfig(
            name="w",
            adapter="postgres",
            output=tmp_path,
            infer_relationships=False,
        )

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(_SkipsOne({"s.t": _table()}), conn, tmp_path).generate()

        warned = [r.getMessage() for r in caplog.records if "could not be listed" in r.getMessage()]

        assert warned == [
            (
                "connection 'w': namespace 'locked' could not be listed and is skipped: "
                "InsufficientPrivilege: denied"
            ),
        ]


class TestTheEngineWarnsOfAnUnreadDependencyNamespace:
    def test_the_namespace_is_named(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        conn = ConnectionConfig(
            name="w",
            adapter="postgres",
            output=tmp_path,
            infer_relationships=False,
        )

        with caplog.at_level(logging.WARNING, logger="dbprint.engine.orchestrator"):
            Engine(_CannotReadOne({"s.t": _table()}), conn, tmp_path).generate()

        warned = [r.getMessage() for r in caplog.records if "view dependencies" in r.getMessage()]

        assert warned == [
            (
                "connection 'w': view dependencies in namespace 'locked' could not be read; its "
                "views omit depends_on this run: InsufficientPrivilege: denied"
            ),
        ]


class _EntryShim:
    def __init__(self, shims: dict[str, RedshiftDialectShim]) -> None:
        self._shims = shims
        self._rows: list[tuple[Any, ...]] = []

    def execute(self, sql_text: str, params: Any = None) -> _EntryShim:
        flat = " ".join(sql_text.lower().split())

        if "svv_redshift_databases" in flat:
            self._rows = [(name,) for name in sorted(self._shims)]
        elif "svv_redshift_tables" in flat or "svv_external_tables" in flat:
            self._rows = [
                row
                for name in sorted(self._shims)
                for row in self._shims[name].execute(sql_text, params).fetchall()
            ]
        elif flat == "select db_collation()":
            self._rows = [("case_sensitive",)]
        else:
            raise AssertionError(f"the entry session ran a per-table statement: {sql_text}")

        return self

    def fetchall(self) -> list[Any]:
        return self._rows

    def fetchone(self) -> Any:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        pass


class _CannotReadOne(MockAdapter):
    def unread_dependency_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return (SkippedNamespace(name="locked", cause="InsufficientPrivilege: denied"),)


class _SkipsOne(MockAdapter):
    def skipped_namespaces(self) -> tuple[SkippedNamespace, ...]:
        return (SkippedNamespace(name="locked", cause="InsufficientPrivilege: denied"),)


def _redshift_stand_ins(
    cluster: PostgresCluster,
    tables: dict[str, str],
) -> Iterator[tuple[str, psycopg.Connection]]:
    admin = cluster
    out = []

    for name, table in tables.items():
        real = f"rs_{name}_{secrets.token_hex(3)}"

        with pg_connect(admin.creds()) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(real)))

        conn = pg_connect(admin.creds(real))
        conn.execute(
            sql.SQL("CREATE TABLE public.{} (id int, tag text)").format(
                sql.Identifier(table),
            ),
        )
        out.append((name, conn))

    return iter(out)


def _table() -> MockTable:
    return mock_table(
        "s.t",
        columns(("id", "int")),
        {},
        ddl="CREATE TABLE s.t (id int);\n",
        row_count=0,
    )


def _postgres_adapter(cluster: PostgresCluster) -> PostgresAdapter:
    return PostgresAdapter(
        {"host": "127.0.0.1", "port": str(cluster.port), "user": "postgres", "password": ""},
    )
