"""Postgres-specific adapter tests + DDL normalization unit tests.

The contract suite in `test_base_contract.py` covers the shared behaviour through
`adapter_factory`; what is left here is postgres-only. DDL normalization is unit-tested
against canned pg_dump output, no DB needed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
import yaml

from dbprint.adapters import ColumnStats, StatisticsConfig, TableCounts, TableScope
from dbprint.adapters.errors import QueryFailed
from dbprint.adapters.postgres import PostgresAdapter, PostgresConnectionError, introspect
from dbprint.adapters.postgres.connection import ConnectionParams, exec_query
from dbprint.adapters.postgres.ddl import extract_ddl, normalize
from dbprint.adapters.postgres.stats import classify_distribution


# Lazy-driver (missing [postgres] extra) unit test (no DB).


class TestMissingExtra:
    """Postgres adapter imports psycopg lazily; an absent driver gives an actionable error."""

    def test_connect_without_psycopg_raises_postgres_extra_hint(self) -> None:
        adapter = PostgresAdapter(
            {"host": "h", "port": "5432", "database": "d", "user": "u", "password": "p"},
        )

        with (
            patch(
                "dbprint.adapters.postgres.connection.importlib.import_module",
                side_effect=ImportError("psycopg not installed"),
            ),
            pytest.raises(PostgresConnectionError, match=r"dbprint\[postgres\]"),
        ):
            adapter.connect()


# DDL normalization unit tests (no DB).


class TestNormalize:
    def test_strips_dump_header_and_footer(self) -> None:
        raw = (
            "--\n"
            "-- PostgreSQL database dump\n"
            "--\n"
            "\n"
            "-- Dumped from database version 16.14\n"
            "-- Dumped by pg_dump version 16.14\n"
            "\n"
            "CREATE TABLE t (id int);\n"
            "\n"
            "--\n"
            "-- PostgreSQL database dump complete\n"
            "--\n"
        )
        out = normalize(raw)
        assert "PostgreSQL database dump" not in out
        assert "Dumped from database version" not in out
        assert "CREATE TABLE t" in out

    def test_strips_set_statements(self) -> None:
        raw = (
            "SET statement_timeout = 0;\n"
            "SET lock_timeout = 0;\n"
            "SET client_encoding = 'UTF8';\n"
            "SET search_path = public;\n"
            "CREATE TABLE t (id int);\n"
        )
        out = normalize(raw)
        assert "SET " not in out
        assert "CREATE TABLE t" in out

    def test_strips_pg_catalog_set_config(self) -> None:
        raw = "SELECT pg_catalog.set_config('search_path', '', false);\nCREATE TABLE t (id int);\n"
        out = normalize(raw)
        assert "pg_catalog.set_config" not in out

    def test_strips_restrict_meta_commands(self) -> None:
        """Their token is regenerated per dump, so keeping them breaks diff stability."""

        first = normalize("\\restrict aAbB01\nCREATE TABLE t (id int);\n\\unrestrict aAbB01\n")
        second = normalize("\\restrict zZyY99\nCREATE TABLE t (id int);\n\\unrestrict zZyY99\n")

        assert "restrict" not in first
        assert first == second
        assert "CREATE TABLE t" in first

    def test_strips_grant_and_revoke_statements(self) -> None:
        raw = (
            "CREATE TABLE t (id int);\n"
            "GRANT SELECT ON TABLE t TO PUBLIC;\n"
            "REVOKE ALL ON SCHEMA public FROM PUBLIC;\n"
        )
        out = normalize(raw)
        assert "GRANT" not in out
        assert "REVOKE" not in out
        assert "CREATE TABLE t" in out

    def test_strips_multi_line_trigger(self) -> None:
        raw = (
            "CREATE TABLE t (id int);\n"
            "CREATE TRIGGER my_trig\n"
            "  AFTER INSERT ON t\n"
            "  FOR EACH ROW EXECUTE FUNCTION my_fn();\n"
            "CREATE INDEX i_idx ON t (id);\n"
        )
        out = normalize(raw)
        assert "CREATE TRIGGER" not in out
        assert "my_fn" not in out
        assert "CREATE INDEX" in out

    def test_strips_create_rule(self) -> None:
        raw = (
            "CREATE TABLE t (id int);\n"
            "CREATE RULE my_rule AS ON UPDATE TO t DO INSTEAD NOTHING;\n"
            "CREATE INDEX i_idx ON t (id);\n"
        )
        out = normalize(raw)
        assert "CREATE RULE" not in out
        assert "CREATE INDEX" in out

    def test_collapses_blank_runs(self) -> None:
        raw = "CREATE TABLE a (id int);\n\n\n\nCREATE TABLE b (id int);\n"
        out = normalize(raw)
        assert "\n\n\n" not in out

    def test_strips_trailing_whitespace(self) -> None:
        raw = "CREATE TABLE t (id int);   \n  more   \n"
        out = normalize(raw)

        for line in out.splitlines():
            assert line == line.rstrip()

    def test_ensures_trailing_newline(self) -> None:
        raw = "CREATE TABLE t (id int);"
        out = normalize(raw)
        assert out.endswith("\n")

    def test_preserves_create_index(self) -> None:
        raw = "CREATE TABLE t (id int);\nCREATE INDEX t_id_idx ON public.t USING btree (id);\n"
        out = normalize(raw)
        assert "CREATE INDEX t_id_idx" in out

    def test_preserves_alter_table_add_constraint(self) -> None:
        raw = (
            "CREATE TABLE t (id int, parent_id int);\n"
            "ALTER TABLE ONLY t ADD CONSTRAINT t_pkey PRIMARY KEY (id);\n"
            "ALTER TABLE ONLY t ADD CONSTRAINT t_parent_fkey FOREIGN KEY (parent_id) REFERENCES t(id);\n"
        )
        out = normalize(raw)
        assert "ALTER TABLE ONLY t ADD CONSTRAINT t_pkey" in out
        assert "ALTER TABLE ONLY t ADD CONSTRAINT t_parent_fkey" in out

    def test_preserves_comment_on(self) -> None:
        raw = (
            "CREATE TABLE t (id int);\n"
            "COMMENT ON TABLE t IS 'my table';\n"
            "COMMENT ON COLUMN t.id IS 'primary key';\n"
        )
        out = normalize(raw)
        assert "COMMENT ON TABLE t" in out
        assert "COMMENT ON COLUMN t.id" in out

    def test_strips_object_name_banners(self) -> None:
        raw = (
            "--\n"
            "-- Name: t; Type: TABLE; Schema: public; Owner: -\n"
            "--\n"
            "\n"
            "CREATE TABLE t (id int);\n"
            "\n"
            "\n"
            "--\n"
            "-- Name: t t_pkey; Type: CONSTRAINT; Schema: public; Owner: -\n"
            "--\n"
            "\n"
            "ALTER TABLE ONLY t\n"
            "    ADD CONSTRAINT t_pkey PRIMARY KEY (id);\n"
        )
        out = normalize(raw)
        assert "-- Name:" not in out
        assert "CREATE TABLE t" in out
        assert "ADD CONSTRAINT t_pkey" in out

    def test_banner_strip_leaves_comment_on_untouched(self) -> None:
        """Only the banner ahead of a COMMENT ON is stripped; `--no-comments` would drop both."""

        raw = (
            "CREATE TABLE t (id int);\n"
            "\n"
            "\n"
            "--\n"
            "-- Name: TABLE t; Type: COMMENT; Schema: public; Owner: -\n"
            "--\n"
            "\n"
            "COMMENT ON TABLE t IS 'my table';\n"
        )
        out = normalize(raw)
        assert "-- Name:" not in out
        assert "COMMENT ON TABLE t IS 'my table';" in out

    def test_strips_orphaned_header_footer_delimiter_pair(self) -> None:
        """Stripping the header/footer leaves `--` delimiters orphaned - drop those too."""

        raw = (
            "--\n"
            "-- PostgreSQL database dump\n"
            "--\n"
            "\n"
            "CREATE TABLE t (id int);\n"
            "\n"
            "--\n"
            "-- PostgreSQL database dump complete\n"
            "--\n"
        )
        out = normalize(raw)
        assert "--" not in out
        assert "CREATE TABLE t" in out


# Catalog-read failure path (no DB) - proves the exec_query seam wraps a driver error.


class _RaisingConnection:
    """Stub connection whose `execute` always raises - proves the seam wraps the failure."""

    def execute(self, query: object, params: object = None) -> object:
        raise RuntimeError("permission denied for schema information_schema")


class TestCatalogReadFailure:
    def test_columns_wraps_the_driver_error_with_the_statement(self) -> None:
        with pytest.raises(QueryFailed, match="permission denied") as exc_info:
            introspect.columns(cast(Any, _RaisingConnection()), "public.t")

        assert "pg_attribute" in exc_info.value.sql


class _StubCursor:
    """Stub cursor carrying a fixed rowcount - stands in for a real driver cursor."""

    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _StubConnection:
    """Stub connection whose `execute` succeeds and returns a fixed-rowcount cursor."""

    def __init__(self, rowcount: int = 3) -> None:
        self._rowcount = rowcount

    def execute(self, query: object, params: object = None) -> object:
        return _StubCursor(self._rowcount)


_TRACE_LOGGER = "dbprint.adapters.postgres.connection"


class TestStatementTrace:
    """exec_query's own DEBUG record - statement, params, elapsed, rows."""

    def test_success_logs_statement_params_and_rows(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger=_TRACE_LOGGER):
            exec_query(cast(Any, _StubConnection(3)), "SELECT %s", ("x",))

        assert "SELECT %s" in caplog.text
        assert "rows=3" in caplog.text

    def test_failure_logs_before_the_exception_propagates(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger=_TRACE_LOGGER),
            pytest.raises(QueryFailed),
        ):
            exec_query(cast(Any, _RaisingConnection()), "SELECT 1")

        assert "statement failed" in caplog.text

    def test_failure_logs_the_statement_past_the_consoles_clip_budget(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        long_sql = "\n".join(f"line {i}" for i in range(40))

        with (
            caplog.at_level(logging.DEBUG, logger=_TRACE_LOGGER),
            pytest.raises(QueryFailed) as exc_info,
        ):
            exec_query(cast(Any, _RaisingConnection()), long_sql)

        assert "line 39" in caplog.text
        assert len(caplog.text) > len(exc_info.value.detail())


# Adapter construction + pg_dump availability.


class TestConstruction:
    def test_rejects_missing_credentials(self) -> None:
        with pytest.raises(PostgresConnectionError, match="missing required credential key"):
            PostgresAdapter({"host": "x", "port": "5432"})

    def test_rejects_non_integer_port(self) -> None:
        with pytest.raises(PostgresConnectionError, match="invalid port"):
            PostgresAdapter(
                {
                    "host": "x",
                    "port": "not-a-number",
                    "database": "d",
                    "user": "u",
                    "password": "p",
                },
            )

    def test_required_keys_class_attr(self) -> None:
        assert PostgresAdapter.REQUIRED_KEYS == ("host", "port", "database", "user", "password")


# Live-Postgres scenarios.


@pytest.fixture
def fresh_postgres(postgres_test_db: dict[str, str]) -> Iterator[PostgresAdapter]:
    adapter = PostgresAdapter(postgres_test_db)
    adapter.connect()
    yield adapter
    adapter.close()


class TestLiveContract:
    def test_lists_seeded_tables(self, fresh_postgres: PostgresAdapter) -> None:
        tables = fresh_postgres.list_tables(include=["*"], exclude=[])
        fqns = {t.fqn for t in tables}
        assert "seedbank.curator" in fqns
        assert "seedbank.herbarium" in fqns

    def test_skips_system_schemas(self, fresh_postgres: PostgresAdapter) -> None:
        tables = fresh_postgres.list_tables(include=["*"], exclude=[])
        assert all(not t.fqn.startswith("pg_") for t in tables)
        assert all(not t.fqn.startswith("information_schema.") for t in tables)

    def test_columns_include_all_seeded(self, fresh_postgres: PostgresAdapter) -> None:
        cols = fresh_postgres.introspect_columns("seedbank.curator")
        names = {c.name for c in cols}
        assert names == {
            "id",
            "email",
            "herbarium_id",
            "is_active",
            "created_at",
            "seed_count",
            "withdrawn_at",
        }

    def test_relationship_emits_array_for_single_column_fk(
        self,
        fresh_postgres: PostgresAdapter,
    ) -> None:
        rels = fresh_postgres.introspect_relationships("seedbank.curator")
        assert len(rels) == 1
        fk = rels[0]
        assert fk.column == ("herbarium_id",)
        assert fk.target_table == "seedbank.herbarium"
        assert fk.target_column == ("id",)
        assert fk.on_delete == "CASCADE"

    def test_index_excludes_pk_and_unique_backed(self, fresh_postgres: PostgresAdapter) -> None:
        idxs = fresh_postgres.introspect_indexes("seedbank.curator")
        names = {i.name for i in idxs}
        assert "curator_email_idx" in names

        for idx in idxs:
            assert not idx.name.endswith("_pkey")

    def test_comments_round_trip(self, fresh_postgres: PostgresAdapter) -> None:
        comments = fresh_postgres.extract_comments("seedbank.curator")
        assert comments.table == "Primary curator table"
        assert comments.columns.get("email") == "user-facing email address"

    def test_ddl_extract_normalized(self, fresh_postgres: PostgresAdapter) -> None:
        ddl = fresh_postgres.extract_ddl("seedbank.curator")
        assert ddl.endswith("\n")
        assert "CREATE TABLE" in ddl
        assert "SET statement_timeout" not in ddl
        assert "PostgreSQL database dump" not in ddl

    def test_sample_values_returns_distinct_non_null(self, fresh_postgres: PostgresAdapter) -> None:
        samples = fresh_postgres.sample_values("seedbank.curator", "email", n=10)
        assert len(set(samples)) == len(samples)
        assert all(s is not None for s in samples)


class TestPhysicalColumnIdentity:
    """SPEC 2.2.1: the `columns` map key is lowercase even where the catalog spelling differs."""

    def _seed_mixed_case(self, creds: dict[str, str]) -> None:
        import psycopg

        with psycopg.connect(
            host=creds["host"],
            port=int(creds["port"]),
            dbname=creds["database"],
            user=creds["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute(
                'CREATE TABLE public.curator (id serial primary key, "fullName" text, '
                '"seedCount" int)',
            )
            conn.execute(
                'INSERT INTO public.curator ("fullName", "seedCount") '
                "SELECT 'name-' || (g % 55), g % 5 FROM generate_series(1, 200) g",
            )
            conn.execute(
                "COMMENT ON COLUMN public.curator.\"fullName\" IS 'full legal name'",
            )
            conn.execute('CREATE INDEX people_fullname_idx ON public.curator ("fullName")')

    def test_the_map_key_is_lowercase_and_the_physical_spelling_is_carried(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed_mixed_case(postgres_test_db)
        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            cols = {c.name: c for c in adapter.introspect_columns("public.curator")}
        finally:
            adapter.close()

        assert cols["fullname"].physical_name == "fullName"
        assert cols["seedcount"].physical_name == "seedCount"
        assert cols["id"].physical_name is None

    def test_the_index_and_comment_keys_agree_with_the_map_key(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed_mixed_case(postgres_test_db)
        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            indexes = adapter.introspect_indexes("public.curator")
            comments = adapter.extract_comments("public.curator")
        finally:
            adapter.close()

        assert any(idx.columns == ("fullname",) for idx in indexes)
        assert comments.columns.get("fullname") == "full legal name"

    def test_statistics_address_the_physical_column(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed_mixed_case(postgres_test_db)
        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            cols = adapter.introspect_columns("public.curator")
            counts, stats = adapter.compute_statistics(
                "public.curator",
                cols,
                StatisticsConfig(),
                frozenset(),
            )
            samples = adapter.sample_values("public.curator", "fullname", n=10)
        finally:
            adapter.close()

        assert counts.row_count == 200
        assert stats["fullname"].cardinality == 55
        assert samples, "sample_values addressed no rows for the quoted column"

    def test_sensitivity_detects_the_camel_case_column_and_a_redact_rule_fires(
        self,
        postgres_test_db: dict[str, str],
        tmp_path: Path,
    ) -> None:
        """Detection reads the catalog's own case, so a redact rule reaches a camelCase column."""

        from dbprint.config.project import ConnectionConfig, DiffConfig, RedactRule
        from dbprint.conformance import validate_print
        from dbprint.engine import Engine

        self._seed_mixed_case(postgres_test_db)
        conn = ConnectionConfig(
            name="primary",
            adapter="postgres",
            auto=False,
            output=tmp_path,
            include=("public.curator",),
            exclude=(),
            max_age_days=7,
            statistics=StatisticsConfig(),
            diff=DiffConfig(),
            redact=(RedactRule(sensitivity=("personal_name",), with_="mask"),),
        )
        adapter = PostgresAdapter(postgres_test_db)
        Engine(adapter, conn, tmp_path).generate()

        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "curator" / "statistics.yaml").read_text(),
        )
        column = stats["columns"]["fullname"]

        assert column["physical_name"] == "fullName"
        assert column["inferred"]["sensitivity"] == "personal_name"
        assert column["redacted"] == "mask"

        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )


class TestCollation:
    """SPEC 2.2.2/2.2.4: `cardinality` and its neighbors are collation-relative."""

    def _seed(self, creds: dict[str, str]) -> None:
        import psycopg

        with psycopg.connect(
            host=creds["host"],
            port=int(creds["port"]),
            dbname=creds["database"],
            user=creds["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute(
                "CREATE TABLE public.labels (id serial primary key, "
                'plain text, forced text COLLATE "C")',
            )
            conn.execute(
                "INSERT INTO public.labels (plain, forced) "
                "SELECT 'v' || (g % 5), 'v' || (g % 5) FROM generate_series(1, 50) g",
            )

    def test_introspection_carries_the_override_and_omits_the_default(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed(postgres_test_db)
        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            cols = {c.name: c for c in adapter.introspect_columns("public.labels")}
            default = adapter.default_collation()
        finally:
            adapter.close()

        assert cols["plain"].collation is None
        assert cols["forced"].collation == "C"
        assert default and default != "C"

    def test_generate_emits_collation_only_where_it_overrides_the_connection_default(
        self,
        postgres_test_db: dict[str, str],
        tmp_path: Path,
    ) -> None:
        from dbprint.config.project import ConnectionConfig, DiffConfig
        from dbprint.conformance import validate_print
        from dbprint.engine import Engine

        self._seed(postgres_test_db)
        conn = ConnectionConfig(
            name="primary",
            adapter="postgres",
            auto=False,
            output=tmp_path,
            include=("public.labels",),
            exclude=(),
            max_age_days=7,
            statistics=StatisticsConfig(),
            diff=DiffConfig(),
        )
        adapter = PostgresAdapter(postgres_test_db)
        Engine(adapter, conn, tmp_path).generate()

        manifest = yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())
        stats = yaml.safe_load(
            (tmp_path / "primary" / "public" / "labels" / "statistics.yaml").read_text(),
        )
        columns = stats["columns"]

        assert manifest["default_collation"] and manifest["default_collation"] != "C"
        assert "collation" not in columns["plain"]
        assert columns["forced"]["collation"] == "C"

        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )


class TestEdgeCases:
    def test_empty_table_yields_zero_stats(self, postgres_test_db: dict[str, str]) -> None:
        import psycopg

        from dbprint.config import StatisticsConfig

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.empty_t (id int, name text)")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            cols = adapter.introspect_columns("public.empty_t")
            _, stats = adapter.compute_statistics(
                "public.empty_t",
                cols,
                StatisticsConfig(),
                frozenset(),
            )
            assert stats["id"].null_count == 0
            assert stats["id"].cardinality == 0
            assert stats["id"].null_rate == 0.0
        finally:
            adapter.close()

    def test_all_null_column(self, postgres_test_db: dict[str, str]) -> None:
        import psycopg

        from dbprint.config import StatisticsConfig

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.nullable_t (id int PRIMARY KEY, opt text)")
            conn.execute("INSERT INTO public.nullable_t (id, opt) VALUES (1, NULL), (2, NULL)")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            cols = adapter.introspect_columns("public.nullable_t")
            _, stats = adapter.compute_statistics(
                "public.nullable_t",
                cols,
                StatisticsConfig(),
                frozenset(),
            )
            assert stats["opt"].null_count == 2
            assert stats["opt"].null_rate == 1.0
            assert stats["opt"].cardinality == 0
        finally:
            adapter.close()

    def test_composite_fk_emits_array(self, postgres_test_db: dict[str, str]) -> None:
        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute(
                """
                CREATE TABLE public.parent (a int, b int, PRIMARY KEY (a, b))
                """,
            )
            conn.execute(
                """
                CREATE TABLE public.child (
                    x int, y int,
                    FOREIGN KEY (x, y) REFERENCES public.parent (a, b)
                )
                """,
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            rels = adapter.introspect_relationships("public.child")
            assert len(rels) == 1
            fk = rels[0]
            assert fk.column == ("x", "y")
            assert fk.target_column == ("a", "b")
            assert fk.target_table == "public.parent"
        finally:
            adapter.close()

    def test_self_referential_fk(self, postgres_test_db: dict[str, str]) -> None:
        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute(
                """
                CREATE TABLE public.curator (
                    id int PRIMARY KEY,
                    mentor_id int REFERENCES public.curator(id)
                )
                """,
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            rels = adapter.introspect_relationships("public.curator")
            assert len(rels) == 1
            assert rels[0].target_table == "public.curator"
            assert rels[0].column == ("mentor_id",)
            assert rels[0].target_column == ("id",)
        finally:
            adapter.close()

    def test_view_listed_but_no_relationships(self, postgres_test_db: dict[str, str]) -> None:
        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.src (id int PRIMARY KEY)")
            conn.execute("CREATE VIEW public.src_v AS SELECT * FROM public.src")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            tables = {t.fqn: t for t in adapter.list_tables(include=["*"], exclude=[])}
            assert tables["public.src_v"].type == "view"
        finally:
            adapter.close()

    def test_matview_listed_with_type(self, postgres_test_db: dict[str, str]) -> None:
        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.src (id int PRIMARY KEY)")
            conn.execute("CREATE MATERIALIZED VIEW public.src_mv AS SELECT * FROM public.src")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            tables = {t.fqn: t for t in adapter.list_tables(include=["*"], exclude=[])}
            assert tables["public.src_mv"].type == "matview"
        finally:
            adapter.close()

    def test_matview_bare_unique_index_is_now_declared_unique(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """The one-directional consequence: `can_be_target` still excludes it on `type`.

        Postgres allows no PRIMARY KEY / UNIQUE constraint on a matview, but a unique index
        on one is reached by the `pg_index` arm, whose join carries no relkind filter.
        """

        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.mv_src (id int PRIMARY KEY, code text NOT NULL)")
            conn.execute(
                "CREATE MATERIALIZED VIEW public.mv_src_mv AS SELECT id, code FROM public.mv_src",
            )
            conn.execute("CREATE UNIQUE INDEX mv_src_mv_code_ux ON public.mv_src_mv (code)")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            groups = {g.columns for g in adapter.introspect_unique_keys("public.mv_src_mv")}
            assert ("code",) in groups
        finally:
            adapter.close()

    def test_varied_types(self, postgres_test_db: dict[str, str]) -> None:
        import psycopg

        from dbprint.config import StatisticsConfig

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute(
                """
                CREATE TABLE public.varied (
                    id uuid PRIMARY KEY,
                    payload jsonb,
                    blob bytea,
                    viability_pct numeric(10, 2),
                    occurred_at timestamp with time zone,
                    flag boolean,
                    label varchar(64)
                )
                """,
            )
            # 200 rows: viability_pct/occurred_at repeat by construction, `id` is unique per row.
            conn.execute(
                """
                INSERT INTO public.varied
                SELECT
                    gen_random_uuid(),
                    jsonb_build_object('k', i),
                    decode(lpad(to_hex(i), 4, '0'), 'hex'),
                    ((i % 60) * 1.25)::numeric(10, 2),
                    timestamp '2025-01-01' + ((i % 80) || ' days')::interval,
                    (i % 2 = 0),
                    'label_' || (i % 10)
                FROM generate_series(1, 200) AS i
                """,
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            cols = adapter.introspect_columns("public.varied")
            _, stats = adapter.compute_statistics(
                "public.varied",
                cols,
                StatisticsConfig(),
                frozenset(),
            )

            # bytea -> unsupported: cardinality/method must be None (SPEC 2.2.3)
            assert stats["blob"].cardinality is None
            assert stats["blob"].cardinality_method is None

            # jsonb -> json pre-classification: stats present, no value list
            assert stats["payload"].values is None
            assert stats["payload"].values_coverage is None

            # numeric -> range + percentiles
            assert stats["viability_pct"].range is not None
            assert stats["viability_pct"].percentiles is not None

            # timestamp -> range with span_days
            assert stats["occurred_at"].range is not None
            assert stats["occurred_at"].range.span_days is not None
            assert stats["occurred_at"].range.max is not None

            # boolean -> values populated
            assert stats["flag"].values is not None
            assert {entry.value for entry in stats["flag"].values} == {True, False}

            # uuid -> text: a unit ratio does not suppress the value list (SPEC 4.2), and
            # `inferred.candidate_key` is the engine's to stamp, not the adapter's.
            assert stats["id"].cardinality_ratio == 1.0
            assert stats["id"].values is not None
            assert stats["id"].range is None
            assert stats["id"].percentiles is None
        finally:
            adapter.close()

    def test_composite_column_declines_measurement(self, postgres_test_db: dict[str, str]) -> None:
        """A composite column measures no cardinality, the same signal bytea gets.

        The dispatch keys on `pg_type.typtype = 'c'`, so a domain wrapping a composite
        falls through the same path.
        """

        import psycopg

        from dbprint.config import StatisticsConfig
        from dbprint.spec.classification import classify

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TYPE public.street_address_t AS (institution text, phone text)")
            conn.execute("CREATE DOMAIN public.street_address_d AS public.street_address_t")
            conn.execute(
                """
                CREATE TABLE public.field_site (
                    id int PRIMARY KEY,
                    addr public.street_address_t,
                    postal_code public.street_address_d,
                    label text
                )
                """,
            )
            conn.execute(
                """
                INSERT INTO public.field_site
                SELECT
                    i,
                    ROW('main st', 'springfield')::public.street_address_t,
                    ROW('main st', 'springfield')::public.street_address_t::public.street_address_d,
                    'v' || i
                FROM generate_series(1, 20) AS i
                """,
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            cols = adapter.introspect_columns("public.field_site")
            _, stats = adapter.compute_statistics(
                "public.field_site",
                cols,
                StatisticsConfig(),
                frozenset(),
            )

            for name in ("addr", "postal_code"):
                assert stats[name].cardinality is None
                assert stats[name].cardinality_method is None
                assert (
                    classify(
                        sql_type=next(c.sql_type for c in cols if c.name == name),
                        cardinality=stats[name].cardinality,
                        has_declared_fk=False,
                        enumeration_threshold=StatisticsConfig().enumeration_threshold,
                    )
                    == "unsupported"
                )

            # A scalar column on the same table measures normally.
            assert stats["label"].cardinality is not None
            assert stats["id"].cardinality is not None
        finally:
            adapter.close()

    def test_bare_unique_index_is_declared_unique_not_an_index(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """Enforced uniqueness is declared, whichever catalog table records it."""

        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.codes (id int PRIMARY KEY, code text NOT NULL)")
            conn.execute("CREATE UNIQUE INDEX codes_code_ux ON public.codes (code)")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            groups = {g.columns for g in adapter.introspect_unique_keys("public.codes")}
            index_columns = {i.columns for i in adapter.introspect_indexes("public.codes")}
            assert ("code",) in groups
            assert ("code",) not in index_columns
        finally:
            adapter.close()

    def test_composite_bare_unique_index_preserves_column_order(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """The union with `pg_constraint` must not scramble a composite key's positions."""

        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute(
                "CREATE TABLE public.pairs (id int PRIMARY KEY, b text NOT NULL, a text NOT NULL)",
            )
            conn.execute("CREATE UNIQUE INDEX pairs_b_a_ux ON public.pairs (b, a)")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            groups = [g.columns for g in adapter.introspect_unique_keys("public.pairs")]
            assert ("b", "a") in groups
        finally:
            adapter.close()

    def test_partial_unique_index_stays_an_ordinary_index(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A partial unique index enforces nothing over the rows it excludes.

        SPEC 2.3.8 requires an unconditional declaration, so a `WHERE` clause keeps the index
        out of `introspect_unique_keys` even though `indisunique` is true.
        """

        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute(
                "CREATE TABLE public.soft_deletes (id int PRIMARY KEY, "
                "code text NOT NULL, deleted_at timestamp)",
            )
            conn.execute(
                "CREATE UNIQUE INDEX soft_deletes_code_ux ON public.soft_deletes (code) "
                "WHERE deleted_at IS NULL",
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            groups = {g.columns for g in adapter.introspect_unique_keys("public.soft_deletes")}
            indexes = {i.columns: i for i in adapter.introspect_indexes("public.soft_deletes")}
            assert ("code",) not in groups
            assert ("code",) in indexes
            assert indexes[("code",)].unique is True
        finally:
            adapter.close()


class TestPhysicalLayout:
    """The declared partition key via `pg_get_partkeydef`, on the parent relation only."""

    def _connect(self, postgres_test_db: dict[str, str]) -> Any:
        import psycopg

        return psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        )

    def test_a_single_column_key_is_recovered(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.curation_event (id int, logged_at date) PARTITION BY RANGE (logged_at)",
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            layout = adapter.introspect_physical_layout("public.curation_event")
            assert layout is not None
            assert layout.mechanism == "partition"
            assert [k.expression for k in layout.keys] == ["logged_at"]
            assert [k.column for k in layout.keys] == ["logged_at"]
        finally:
            adapter.close()

    def test_a_multi_column_key_preserves_declaration_order(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.field_log (country_code text, logged_at date) "
                "PARTITION BY RANGE (country_code, logged_at)",
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            layout = adapter.introspect_physical_layout("public.field_log")
            assert layout is not None
            assert [k.column for k in layout.keys] == ["country_code", "logged_at"]
        finally:
            adapter.close()

    def test_an_expression_key_carries_no_base_column(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A function-call key names no single column a predicate matches on."""

        with self._connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.logs (logged_at timestamp) "
                "PARTITION BY RANGE (date_trunc('day', logged_at))",
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            layout = adapter.introspect_physical_layout("public.logs")
            assert layout is not None
            key = layout.keys[0]
            assert key.column is None
            assert "date_trunc" in key.expression
        finally:
            adapter.close()

    def test_hash_partitioning_reports_as_the_same_mechanism(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """RANGE/LIST/HASH are all `partition` - dbprint names the class, not the strategy."""

        with self._connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.buckets (id int) PARTITION BY HASH (id)")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            layout = adapter.introspect_physical_layout("public.buckets")
            assert layout is not None
            assert layout.mechanism == "partition"
            assert [k.column for k in layout.keys] == ["id"]
        finally:
            adapter.close()

    def test_an_unpartitioned_table_reports_absence(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.plain (id int)")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            assert adapter.introspect_physical_layout("public.plain") is None
        finally:
            adapter.close()

    def test_an_individual_partition_is_not_queried_for_its_own_key(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """Only the partitioned parent (`relkind = 'p'`) carries a key; a child inherits it."""

        with self._connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.specimen_loan (id int, logged_at date) PARTITION BY RANGE (logged_at)",
            )
            conn.execute(
                "CREATE TABLE public.specimen_loan_2024 PARTITION OF public.specimen_loan "
                "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            assert adapter.introspect_physical_layout("public.specimen_loan_2024") is None
        finally:
            adapter.close()


class TestPartitionChildExclusion:
    """`list_tables` enumerates a partitioned table's parent, never its children."""

    def _connect(self, postgres_test_db: dict[str, str]) -> Any:
        import psycopg

        return psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        )

    def test_attached_partitions_collapse_to_the_parent_alone(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.viability_check (id int, logged_at date) PARTITION BY RANGE (logged_at)",
            )
            conn.execute(
                "CREATE TABLE public.readings_2024 PARTITION OF public.viability_check "
                "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
            )
            conn.execute(
                "CREATE TABLE public.readings_2025 PARTITION OF public.viability_check "
                "FOR VALUES FROM ('2025-01-01') TO ('2026-01-01')",
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            fqns = {t.fqn for t in adapter.list_tables(include=["*"], exclude=[])}
            assert "public.viability_check" in fqns
            assert "public.readings_2024" not in fqns
            assert "public.readings_2025" not in fqns
        finally:
            adapter.close()

    def test_a_detached_former_partition_enumerates_normally(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.batches (id int, logged_at date) PARTITION BY RANGE (logged_at)",
            )
            conn.execute(
                "CREATE TABLE public.batches_2024 PARTITION OF public.batches "
                "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
            )
            conn.execute("ALTER TABLE public.batches DETACH PARTITION public.batches_2024")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            fqns = {t.fqn for t in adapter.list_tables(include=["*"], exclude=[])}
            assert "public.batches" in fqns
            assert "public.batches_2024" in fqns
        finally:
            adapter.close()


class TestIdentifierRejection:
    """SPEC 1.5: producers reject identifiers that violate the path-segment allowlist."""

    def test_unsafe_character_rejected(self, postgres_test_db: dict[str, str]) -> None:
        import psycopg

        from dbprint.adapters.postgres.introspect import IdentifierRejected

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            # Quoted identifier with a space lowercases to "weird name" - fails the regex.
            conn.execute('CREATE TABLE public."weird name" (id int)')

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            with pytest.raises(IdentifierRejected) as exc_info:
                adapter.list_tables(include=["*"], exclude=[])

            message = str(exc_info.value)
            assert "contains-unsafe-character" in message
            assert "Resolution:" in message
            assert "exclude:" in message
        finally:
            adapter.close()

    def test_excluded_unsafe_identifier_does_not_block(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """Per SPEC 1.5.5: excluding the bad table via selectors lets the run proceed."""

        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute('CREATE TABLE public."weird name" (id int)')
            conn.execute("CREATE TABLE public.ok (id int)")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            tables = adapter.list_tables(include=["*"], exclude=["public.weird name"])
            fqns = {t.fqn for t in tables}
            assert "public.ok" in fqns
            assert "public.weird name" not in fqns
        finally:
            adapter.close()

    def test_case_collision_rejected(self, postgres_test_db: dict[str, str]) -> None:
        """SPEC 1.5.2: two identifiers that lowercase to the same path abort the run."""

        import psycopg

        from dbprint.adapters.postgres.introspect import IdentifierRejected

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute('CREATE TABLE public."Curator" (id int)')
            conn.execute("CREATE TABLE public.curator (id int)")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            with pytest.raises(IdentifierRejected) as exc_info:
                adapter.list_tables(include=["*"], exclude=[])

            message = str(exc_info.value)
            # Either row can be the "previous" entry - catalog order is a cluster-collation
            # detail - so assert both names appear without fixing which is which.
            assert "case-collides-with-public." in message
            assert "public.Curator" in message
            assert "public.curator" in message
            assert "Resolution:" in message
        finally:
            adapter.close()

    def test_excluded_case_collision_lets_the_run_proceed(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """Per SPEC 1.5.4: excluding the pair's shared path resolves the collision.

        Selectors match the lowercased FQN, so excluding it drops both candidates rather
        than picking a survivor; the unrelated table proves the run still completes.
        """

        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute('CREATE TABLE public."Curator" (id int)')
            conn.execute("CREATE TABLE public.curator (id int)")
            conn.execute("CREATE TABLE public.ok (id int)")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            tables = adapter.list_tables(include=["public.*"], exclude=["public.curator"])
            assert [t.fqn for t in tables] == ["public.ok"]
        finally:
            adapter.close()


class TestPgDumpMissing:
    def test_raises_when_pg_dump_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import dbprint.adapters.postgres.connection as connection_mod

        monkeypatch.setattr(connection_mod.shutil, "which", lambda _: None)
        adapter = PostgresAdapter(
            {"host": "x", "port": "5432", "database": "d", "user": "u", "password": "p"},
        )

        with pytest.raises(PostgresConnectionError, match="pg_dump"):
            adapter.connect()


class TestPgDumpTrace:
    """pg_dump traced as an invocation: argv + elapsed, credentials excluded."""

    def test_argv_traced_and_credentials_absent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import subprocess as subprocess_mod

        import dbprint.adapters.postgres.ddl as ddl_mod

        def _fake_run(argv: list[str], **_kwargs: Any) -> subprocess_mod.CompletedProcess[str]:
            return subprocess_mod.CompletedProcess(
                argv,
                0,
                stdout="CREATE TABLE public.t (id int);\n",
                stderr="",
            )

        monkeypatch.setattr(ddl_mod.subprocess, "run", _fake_run)
        params = ConnectionParams(
            host="h",
            port=5432,
            database="d",
            user="u",
            password="s3cr3t-password",
        )

        with caplog.at_level(logging.DEBUG, logger="dbprint.adapters.postgres.ddl"):
            extract_ddl(params, "public.t")

        assert "pg_dump" in caplog.text
        assert "--table=public.t" in caplog.text
        assert "s3cr3t-password" not in caplog.text
        assert "PGPASSWORD" not in caplog.text


class TestExecuteQueryTrace:
    """The SQL-assertion seam (`check --online`'s `sql:` block) is traced like any statement."""

    def test_the_operators_statement_is_traced(
        self,
        fresh_postgres: PostgresAdapter,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="dbprint.adapters.postgres.connection"):
            fresh_postgres.execute_query("SELECT 1 AS n")

        assert "SELECT 1 AS n" in caplog.text
        assert "rows=1" in caplog.text


class _RecordingConnection:
    """Wraps a real psycopg connection; records every statement text verbatim."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.statements: list[str] = []

    def execute(self, query: object, params: object = None) -> object:
        self.statements.append(str(query))

        return self._real.execute(query, params)


class TestHashOrderedDraw:
    """SPEC 4.1.2: the distinct draw is ordered by a hash of the value, not storage order."""

    def test_the_draw_is_sql_ordered_by_a_hash_of_the_value(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """Asserts on the emitted statement text, which the behavioral checks cannot prove."""

        import psycopg

        from dbprint.adapters.postgres import looks_like as postgres_looks_like

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.sql_shape (v text)")
            conn.execute(
                "INSERT INTO public.sql_shape SELECT 'val-' || i FROM generate_series(1, 100) i",
            )

            recorder = _RecordingConnection(conn)
            postgres_looks_like.sample_distinct(
                cast(psycopg.Connection, recorder),
                "public.sql_shape",
                "v",
                n=50,
            )
            flat = " ".join(" ".join(s.lower().split()) for s in recorder.statements)

            assert "order by" in flat and "md5(" in flat, (
                f"expected the distinct draw ordered by a hash of the value; "
                f"captured SQL: {recorder.statements}"
            )

    def test_a_value_inserted_last_is_still_reachable(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """5,000 rows stays on the small path, where n=1000 clears `n * SMALL_TABLE_FACTOR`."""

        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.late_shape (v text)")
            conn.execute(
                "INSERT INTO public.late_shape SELECT 'row-' || i FROM generate_series(1, 4000) i",
            )
            conn.execute(
                "INSERT INTO public.late_shape "
                "SELECT '11111111-1111-4111-8111-' || lpad(i::text, 12, '0') "
                "FROM generate_series(1, 1000) i",
            )
            # The dispatch reads this estimate; 5,000 rows then clears n * SMALL_TABLE_FACTOR.
            conn.execute("ANALYZE public.late_shape")

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            values = adapter.sample_values("public.late_shape", "v", n=1000)
            late = [v for v in values if str(v).startswith("11111111-1111-4111-8111-")]

            assert late, "the draw never reached a value inserted after the first n rows"
        finally:
            adapter.close()

    def test_two_draws_over_unchanged_data_agree(self, postgres_test_db: dict[str, str]) -> None:
        """The hash order is a function of the table's own seed, not session state."""

        import psycopg

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.stable_draw (v text)")
            conn.execute(
                "INSERT INTO public.stable_draw SELECT 'val-' || i FROM generate_series(1, 5000) i",
            )

        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            first = adapter.sample_values("public.stable_draw", "v", n=50)
            second = adapter.sample_values("public.stable_draw", "v", n=50)

            assert first == second
        finally:
            adapter.close()


class TestApproximateCardinality:
    """Above the row threshold, the adapter estimates distinct counts instead of counting them.

    Postgres encodes a high-cardinality column as a NEGATIVE `n_distinct`, the negated
    fraction of rows, so the estimate must be decoded against the row count; read as an
    absolute count it sends the column into the categorical branch's unbounded GROUP BY.
    """

    @staticmethod
    def _profile(creds: dict[str, str], fqn: str, threshold: int) -> dict[str, ColumnStats]:
        from dbprint.adapters.postgres import stats as pg_stats
        from dbprint.config import StatisticsConfig

        adapter = PostgresAdapter(creds)
        adapter.connect()

        try:
            cols = adapter.introspect_columns(fqn)

            with patch.object(pg_stats, "APPROXIMATE_THRESHOLD", threshold):
                _, computed = adapter.compute_statistics(
                    fqn,
                    cols,
                    StatisticsConfig(),
                    frozenset(),
                )

            return computed
        finally:
            adapter.close()

    @staticmethod
    def _seed(creds: dict[str, str], *, analyze: bool) -> None:
        """500 rows: `id` unique (negative n_distinct), `label` four values."""

        import psycopg

        with psycopg.connect(
            host=creds["host"],
            port=int(creds["port"]),
            dbname=creds["database"],
            user=creds["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.wide_t (id int, label text)")
            conn.execute(
                "INSERT INTO public.wide_t "
                "SELECT g, 'label_' || (g % 4) FROM generate_series(1, 500) g",
            )

            if analyze:
                conn.execute("ANALYZE public.wide_t")

    def test_negative_n_distinct_is_decoded_against_the_row_count(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A unique column gets n_distinct = -1, meaning every row is distinct."""

        self._seed(postgres_test_db, analyze=True)
        stats = self._profile(postgres_test_db, "public.wide_t", threshold=10)

        # Every value distinct: the estimate must land on the row count, not zero, and a
        # ratio of 1.0 reaches the near-unique re-probe, which counts it exactly.
        assert stats["id"].cardinality == 500
        assert stats["id"].cardinality_ratio == 1.0
        assert stats["id"].cardinality_method == "exact"

        # A small enumeration stays under the re-probe ratio and keeps its positive estimate.
        assert stats["label"].cardinality == 4
        assert stats["label"].cardinality_method == "approximate"

    def test_estimated_column_does_not_take_the_enumeration_branch(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A near-unique numeric column keeps its bounds rather than enumerating values."""

        self._seed(postgres_test_db, analyze=True)
        stats = self._profile(postgres_test_db, "public.wide_t", threshold=10)

        assert stats["id"].values is None, "numeric never enumerates values (SPEC 2.2.3)"
        assert stats["id"].range is not None
        assert (stats["id"].cardinality_ratio or 0) >= 0.9999

    def test_unanalyzed_column_falls_back_to_the_exact_count(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """No planner row is the absence of an estimate, not a zero."""

        self._seed(postgres_test_db, analyze=False)
        stats = self._profile(postgres_test_db, "public.wide_t", threshold=10)

        assert stats["id"].cardinality == 500
        assert stats["id"].cardinality_method == "exact"

    def test_below_the_threshold_reports_exact(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed(postgres_test_db, analyze=True)
        stats = self._profile(postgres_test_db, "public.wide_t", threshold=100_000)

        assert stats["id"].cardinality == 500
        assert stats["id"].cardinality_method == "exact"


class TestStaleEstimateCannotUnboundTheRead:
    """A wrong estimate must not turn into an unbounded enumeration.

    An estimate under `enumeration_threshold` while the truth is far above still routes the
    column into the categorical branch, so the query, not the estimate, has to bound it. A
    capped list looks the same either way, so these tests assert on the statement.
    """

    @staticmethod
    def _seed(creds: dict[str, str]) -> None:
        """Analyze on four labels, then add two thousand more without re-analyzing."""

        import psycopg

        with psycopg.connect(
            host=creds["host"],
            port=int(creds["port"]),
            dbname=creds["database"],
            user=creds["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.stale_t (label text)")
            conn.execute(
                "INSERT INTO public.stale_t SELECT 'label_' || (g % 4) "
                "FROM generate_series(1, 200) g",
            )
            conn.execute("ANALYZE public.stale_t")
            conn.execute(
                "INSERT INTO public.stale_t SELECT 'unique_' || g FROM generate_series(1, 2000) g",
            )

    @staticmethod
    def _profile(creds: dict[str, str]) -> tuple[dict[str, ColumnStats], list[str]]:
        from dbprint.adapters.postgres import stats as pg_stats
        from dbprint.config import StatisticsConfig
        from tests.adapters.test_dialect_guard import _install_recorder

        adapter = PostgresAdapter(creds)
        adapter.connect()
        recorder = _install_recorder(adapter)

        try:
            cols = adapter.introspect_columns("public.stale_t")

            with patch.object(pg_stats, "APPROXIMATE_THRESHOLD", 10):
                _, computed = adapter.compute_statistics(
                    "public.stale_t",
                    cols,
                    StatisticsConfig(),
                    frozenset(),
                )

            return computed, recorder.flattened()
        finally:
            adapter.close()

    def test_the_stale_estimate_really_is_wrong(self, postgres_test_db: dict[str, str]) -> None:
        """The precondition: without this the rest of the class proves nothing."""

        self._seed(postgres_test_db)
        stats, _ = self._profile(postgres_test_db)

        label = stats["label"]

        assert label.cardinality_method == "approximate"
        assert label.cardinality is not None
        assert label.cardinality <= 50, "the estimate did not land under the threshold"
        assert label.values is not None, "the column did not take the enumeration branch"

    def test_the_enumeration_query_is_bounded(self, postgres_test_db: dict[str, str]) -> None:
        self._seed(postgres_test_db)
        _, statements = self._profile(postgres_test_db)
        grouped = [s for s in statements if "group by" in s]
        unbounded = [s for s in grouped if "limit" not in s]

        assert grouped, "no grouping statement ran; the check would be vacuous"
        assert not unbounded, f"a wrong estimate produced an unbounded read: {unbounded}"

    def test_the_artifact_does_not_claim_a_domain_it_truncated(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        from dbprint.config import StatisticsConfig

        self._seed(postgres_test_db)
        stats, _ = self._profile(postgres_test_db)
        label = stats["label"]

        assert label.values is not None
        assert len(label.values) <= StatisticsConfig().top_n_values
        assert label.values_coverage is not None
        assert label.values_coverage < 1.0, "a truncated list must not read as exhaustive"


class TestApproximateEstimateBoundedByNonNullCount:
    """`cardinality` is defined over non-null values alone (SPEC 2.2.2) - the clamp must match.

    A stale `n_distinct = -1` scales with the fresh `COUNT(*)`, nulls included, so nulls added
    after `ANALYZE` inflate the estimate; clamping to `row_count` would publish a ratio of 1.0
    on a column that is one-third null.
    """

    @staticmethod
    def _seed(creds: dict[str, str]) -> None:
        """1000 unique values, analyzed, then 500 nulls added without re-analyzing."""

        import psycopg

        with psycopg.connect(
            host=creds["host"],
            port=int(creds["port"]),
            dbname=creds["database"],
            user=creds["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.null_heavy_t (val int)")
            conn.execute(
                "INSERT INTO public.null_heavy_t SELECT g FROM generate_series(1, 1000) g",
            )
            conn.execute("ANALYZE public.null_heavy_t")
            conn.execute(
                "INSERT INTO public.null_heavy_t SELECT NULL FROM generate_series(1, 500) g",
            )

    @staticmethod
    def _profile(creds: dict[str, str]) -> ColumnStats:
        from dbprint.adapters.postgres import stats as pg_stats
        from dbprint.config import StatisticsConfig

        adapter = PostgresAdapter(creds)
        adapter.connect()

        try:
            cols = adapter.introspect_columns("public.null_heavy_t")

            with patch.object(pg_stats, "APPROXIMATE_THRESHOLD", 10):
                _, computed = adapter.compute_statistics(
                    "public.null_heavy_t",
                    cols,
                    StatisticsConfig(),
                    frozenset(),
                )

            return computed["val"]
        finally:
            adapter.close()

    def test_the_stale_estimate_really_would_overshoot(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """The precondition: without this the rest of the class proves nothing."""

        self._seed(postgres_test_db)
        stats = self._profile(postgres_test_db)

        assert stats.cardinality_method == "approximate"
        assert stats.null_count == 500

    def test_cardinality_does_not_exceed_the_non_null_count(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed(postgres_test_db)
        stats = self._profile(postgres_test_db)

        assert stats.cardinality is not None
        assert stats.cardinality <= 1000, f"1500 - 500 non-null rows exceeded: {stats.cardinality}"

    def test_the_ratio_does_not_cross_the_candidate_key_boundary(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A `min(row_count, estimate)` clamp would land this ratio at exactly 1.0 - SPEC 4.2."""

        self._seed(postgres_test_db)
        stats = self._profile(postgres_test_db)

        assert stats.cardinality_ratio is not None
        assert stats.cardinality_ratio < 0.9999, (
            f"a heavily-null column crossed the candidate-key boundary: {stats.cardinality_ratio}"
        )


class TestScopedStatistics:
    """A narrowed read reports what it actually read (SPEC 2.2.8)."""

    @staticmethod
    def _seed(creds: dict[str, str], *, analyze: bool = False) -> None:
        import psycopg

        with psycopg.connect(
            host=creds["host"],
            port=int(creds["port"]),
            dbname=creds["database"],
            user=creds["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.scoped_t (id int, bucket int)")
            conn.execute(
                "INSERT INTO public.scoped_t SELECT g, g % 4 FROM generate_series(1, 400) g",
            )

            # reltuples is -1 until analyzed; without this every draw takes the direct read.
            if analyze:
                conn.execute("ANALYZE public.scoped_t")

    @staticmethod
    def _sample(creds: dict[str, str], n: int, scope: TableScope | None) -> list:
        adapter = PostgresAdapter(creds)
        adapter.connect()

        try:
            return adapter.sample_values("public.scoped_t", "id", n, scope)
        finally:
            adapter.close()

    @staticmethod
    def _profile(creds: dict[str, str], scope: TableScope | None) -> tuple[TableCounts, dict]:
        from dbprint.config import StatisticsConfig

        adapter = PostgresAdapter(creds)
        adapter.connect()

        try:
            cols = adapter.introspect_columns("public.scoped_t")

            return adapter.compute_statistics(
                "public.scoped_t",
                cols,
                StatisticsConfig(),
                frozenset(),
                None,
                scope,
            )
        finally:
            adapter.close()

    def test_unscoped_reports_the_whole_table(self, postgres_test_db: dict[str, str]) -> None:
        self._seed(postgres_test_db)
        counts, _ = self._profile(postgres_test_db, None)

        assert counts.row_count == 400
        assert counts.rows_scanned == 400
        assert counts.row_count_method == "exact"

    def test_a_narrowed_read_with_no_estimate_counts_and_says_so(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """The one branch where narrowing and estimating part company.

        `reltuples` is -1 until the table is analyzed, so the read falls back to an exact
        `COUNT(*)`, which must not be reported as an estimate.
        """

        self._seed(postgres_test_db)
        counts, _ = self._profile(postgres_test_db, TableScope(sample=0.25))

        assert counts.row_count == 400
        assert counts.row_count_method == "exact"

    def test_a_narrowed_read_with_an_estimate_says_approximate(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """The ordinary narrowed read, which takes the planner's number."""

        self._seed(postgres_test_db, analyze=True)
        counts, _ = self._profile(postgres_test_db, TableScope(sample=0.25))

        assert counts.row_count_method == "approximate"

    def test_a_filter_scans_exactly_what_it_matches(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed(postgres_test_db)
        counts, stats = self._profile(
            postgres_test_db,
            TableScope(filter="bucket = 0"),
        )

        assert counts.rows_scanned == 100
        # Every count is relative to the scanned set, so the surviving bucket is the domain.
        assert stats["bucket"].cardinality == 1

    def test_ratios_use_the_scanned_set_as_denominator(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A ratio against the table total reads below 1.0 and misleads."""

        self._seed(postgres_test_db)
        counts, stats = self._profile(
            postgres_test_db,
            TableScope(filter="bucket = 0"),
        )

        assert counts.rows_scanned == 100
        assert stats["id"].cardinality_ratio == 1.0

    def test_a_filter_matching_nothing_is_not_an_empty_table(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """SPEC 2.2.7 keeps the two cases distinct; row_count survives."""

        self._seed(postgres_test_db)
        counts, _ = self._profile(
            postgres_test_db,
            TableScope(filter="bucket = 99"),
        )

        assert counts.rows_scanned == 0
        assert counts.row_count > 0

    def test_a_sample_reads_a_fraction(self, postgres_test_db: dict[str, str]) -> None:
        self._seed(postgres_test_db)
        counts, _ = self._profile(postgres_test_db, TableScope(sample=0.25))

        # Bernoulli is per row, so on 400 rows this asserts it sampled at all, not the fraction.
        assert 0 < counts.rows_scanned < 400

    def test_a_filter_binds_to_the_sampled_values(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """`bucket = 0` leaves the multiples of four, so any other value escaped the scope."""

        self._seed(postgres_test_db)
        values = self._sample(postgres_test_db, 50, TableScope(filter="bucket = 0"))

        assert values
        assert all(v % 4 == 0 for v in values)

    def test_the_same_call_unscoped_reaches_the_other_buckets(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """Negative control: without the scope the assertion above would not hold."""

        self._seed(postgres_test_db)
        values = self._sample(postgres_test_db, 50, None)

        assert any(v % 4 != 0 for v in values)

    def test_a_filter_binds_on_the_sampling_path_too(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """The estimate clears the cutoff, so this is the sub-draw branch.

        The starved draw also fires the re-read, which must stay inside the scope.
        """

        self._seed(postgres_test_db, analyze=True)
        values = self._sample(postgres_test_db, 5, TableScope(filter="bucket = 0"))

        assert values
        assert all(v % 4 == 0 for v in values)

    @staticmethod
    def _sample_statements(creds: dict[str, str], n: int, scope: TableScope | None) -> list[str]:
        from tests.adapters.test_dialect_guard import _install_recorder

        adapter = PostgresAdapter(creds)
        adapter.connect()
        recorder = _install_recorder(adapter)

        try:
            adapter.sample_values("public.scoped_t", "bucket", n, scope)

            return [s for s in recorder.flattened() if "select distinct" in s]
        finally:
            adapter.close()

    def test_an_unscoped_thin_column_is_not_re_read(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """Thin because the column IS thin, not the draw; a re-read buys a whole-table scan."""

        self._seed(postgres_test_db, analyze=True)
        statements = self._sample_statements(postgres_test_db, 5, None)

        assert len(statements) == 1, f"the unscoped draw was re-read: {statements}"

    def test_a_starved_filtered_draw_is_re_read(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """The positive control: a predicate is the case the re-read exists for."""

        self._seed(postgres_test_db, analyze=True)
        statements = self._sample_statements(postgres_test_db, 5, TableScope(filter="bucket = 0"))

        assert len(statements) == 2, f"the starved draw was not re-read: {statements}"

    def test_a_sampled_scope_bounds_the_draw(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A fraction narrows what the sampler may see, so it cannot return more."""

        self._seed(postgres_test_db, analyze=True)
        values = self._sample(postgres_test_db, 50, TableScope(sample=0.25))

        assert len(values) < 400


class TestDatelessTemporal:
    """A time of day has no date, so `max_age_days` cannot be derived from it (SPEC 2.2.4).

    `spec.temporal_age.parse_instant` reads a bare clock reading as unparseable, so the
    derived age is 0 without any arithmetic.
    """

    @staticmethod
    def _profile(creds: dict[str, str]) -> dict[str, ColumnStats]:
        import psycopg

        from dbprint.config import StatisticsConfig

        with psycopg.connect(
            host=creds["host"],
            port=int(creds["port"]),
            dbname=creds["database"],
            user=creds["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.field_round (id int, run_at time)")
            # 60 distinct values clear the enumeration threshold, so the column is temporal.
            conn.execute(
                "INSERT INTO public.field_round "
                "SELECT g, TIME '08:00:00' + (g % 60) * INTERVAL '1 minute' "
                "FROM generate_series(1, 200) g",
            )

        adapter = PostgresAdapter(creds)
        adapter.connect()

        try:
            cols = adapter.introspect_columns("public.field_round")
            _, stats = adapter.compute_statistics(
                "public.field_round",
                cols,
                StatisticsConfig(),
                frozenset(),
            )

            return stats
        finally:
            adapter.close()

    def test_span_is_zero_because_every_value_sits_inside_one_day(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        stats = self._profile(postgres_test_db)["run_at"]

        assert stats.range is not None
        assert stats.range.span_days == 0

    def test_range_renders_in_the_columns_own_domain(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A clock reading round-trips into a predicate; a date does not."""

        stats = self._profile(postgres_test_db)["run_at"]

        assert stats.range is not None
        assert stats.range.min == "08:00:00"
        assert stats.range.max == "08:59:00"


class TestOutOfRangeTemporal:
    """A year outside 0001-9999, or `infinity`, does not take the table down.

    `_fetch_temporal_block` renders bounds to text in SQL, so psycopg never has to build a
    `datetime` it refuses to construct.
    """

    @staticmethod
    def _seed(creds: dict[str, str], values: list[str]) -> None:
        import psycopg

        with psycopg.connect(
            host=creds["host"],
            port=int(creds["port"]),
            dbname=creds["database"],
            user=creds["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("SET TimeZone = 'UTC'")
            conn.execute("CREATE TABLE public.viability_check (id int, taken_at timestamptz)")
            # 60 distinct minutes clear the enumeration threshold, so the column is temporal.
            rows = [(i, f"2020-01-01 00:{i % 60:02d}:00+00") for i in range(240)]
            rows.extend((240 + i, v) for i, v in enumerate(values))

            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO public.viability_check (id, taken_at) VALUES (%s, %s)",
                    rows,
                )

    @staticmethod
    def _profile(creds: dict[str, str]) -> dict[str, ColumnStats]:
        from dbprint.config import StatisticsConfig

        adapter = PostgresAdapter(creds)
        adapter.connect()

        try:
            cols = adapter.introspect_columns("public.viability_check")
            _, stats = adapter.compute_statistics(
                "public.viability_check",
                cols,
                StatisticsConfig(),
                frozenset(),
            )

            return stats
        finally:
            adapter.close()

    def test_year_beyond_9999_profiles_and_is_marked(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed(postgres_test_db, ["52030-01-01 00:00:00+00"])
        stats = self._profile(postgres_test_db)["taken_at"]

        assert stats.range is not None
        assert stats.range.max == "52030-01-01T00:00:00Z"
        assert stats.unrepresentable == ("max",)

    def test_year_at_or_below_zero_is_marked(self, postgres_test_db: dict[str, str]) -> None:
        self._seed(postgres_test_db, ["4713-01-01 00:00:00+00 BC"])
        stats = self._profile(postgres_test_db)["taken_at"]

        assert stats.range is not None
        assert stats.range.min == "4713-01-01T00:00:00Z BC"
        assert stats.unrepresentable == ("min",)

    def test_infinity_sentinel_profiles_and_is_marked(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed(postgres_test_db, ["infinity"])
        stats = self._profile(postgres_test_db)["taken_at"]

        assert stats.range is not None
        assert stats.range.max == "infinity"
        assert stats.unrepresentable == ("max",)
        # A finite, clamped approximation, not the overflow psycopg raises on an infinite bound.
        assert stats.range.span_days is not None
        assert stats.range.span_days > 0

    def test_ordinary_values_render_byte_identical(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """Ordinary values are unaffected by the out-of-range handling above."""

        self._seed(postgres_test_db, [])
        stats = self._profile(postgres_test_db)["taken_at"]

        assert stats.range is not None
        assert stats.range.min == "2020-01-01T00:00:00Z"
        assert stats.range.max == "2020-01-01T00:59:00Z"
        assert stats.unrepresentable is None

    def test_rendering_is_independent_of_session_timezone(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        import psycopg

        from dbprint.adapters.base import ColumnMeta
        from dbprint.adapters.postgres import stats as pg_stats
        from dbprint.config import StatisticsConfig

        self._seed(postgres_test_db, [])
        config = StatisticsConfig()
        col = ColumnMeta(
            name="taken_at",
            sql_type="timestamp with time zone",
            nullable=True,
            default=None,
            ordinal=1,
        )

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("SET TimeZone = 'UTC'")
            utc_rng, *_ = pg_stats._fetch_temporal_block(
                conn,
                "public.viability_check",
                col,
                60,
                config,
            )

            conn.execute("SET TimeZone = 'America/New_York'")
            shifted_rng, *_ = pg_stats._fetch_temporal_block(
                conn,
                "public.viability_check",
                col,
                60,
                config,
            )

        assert shifted_rng.min == utc_rng.min
        assert shifted_rng.max == utc_rng.max

    def test_value_list_rendering_is_independent_of_session_timezone(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A low-cardinality timestamptz's `values` must agree with `range` on the frame."""

        import psycopg

        from dbprint.adapters.base import ColumnMeta
        from dbprint.adapters.postgres import stats as pg_stats
        from dbprint.config import StatisticsConfig

        self._seed(postgres_test_db, [])
        config = StatisticsConfig()
        col = ColumnMeta(
            name="taken_at",
            sql_type="timestamp with time zone",
            nullable=True,
            default=None,
            ordinal=1,
        )

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("SET TimeZone = 'UTC'")
            utc_values, *_ = pg_stats._fetch_value_list(
                conn,
                "public.viability_check",
                col,
                240,
                config,
            )

            conn.execute("SET TimeZone = 'America/New_York'")
            shifted_values, *_ = pg_stats._fetch_value_list(
                conn,
                "public.viability_check",
                col,
                240,
                config,
            )

        assert shifted_values == utc_values
        assert all(entry.value.endswith("Z") for entry in utc_values)

    def test_degradation_net_drops_bounds_not_the_table(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed(postgres_test_db, [])

        with patch(
            "dbprint.adapters.postgres.stats._fetch_calendar_temporal_block",
            side_effect=RuntimeError("simulated failure"),
        ):
            stats = self._profile(postgres_test_db)

        assert stats["taken_at"].range is None
        assert stats["taken_at"].percentiles is None
        assert stats["taken_at"].unrepresentable is None
        # The rest of the column's statistics, and every other column, survive.
        assert stats["taken_at"].cardinality is not None
        assert stats["id"].cardinality is not None

    def test_empty_non_null_set_does_not_clamp_to_a_bogus_span(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """`GREATEST`/`LEAST` ignore a NULL argument rather than propagating it.

        Called directly rather than through `compute_statistics`: an all-NULL column has
        cardinality 0, so the classifier never routes it to the temporal branch.
        """
        import psycopg

        from dbprint.adapters.base import ColumnMeta
        from dbprint.adapters.postgres import stats as pg_stats
        from dbprint.config import StatisticsConfig

        with psycopg.connect(
            host=postgres_test_db["host"],
            port=int(postgres_test_db["port"]),
            dbname=postgres_test_db["database"],
            user=postgres_test_db["user"],
            password="",
            autocommit=True,
        ) as conn:
            conn.execute("CREATE TABLE public.all_null_ts (id int, taken_at timestamptz)")
            conn.execute("INSERT INTO public.all_null_ts (id, taken_at) VALUES (1, NULL)")

            col = ColumnMeta(
                name="taken_at",
                sql_type="timestamp with time zone",
                nullable=True,
                default=None,
                ordinal=1,
            )
            rng, percentiles, _, unrepresentable, _ = pg_stats._fetch_temporal_block(
                conn,
                "public.all_null_ts",
                col,
                0,
                StatisticsConfig(),
            )

        assert rng.min is None
        assert rng.max is None
        assert rng.span_days == 0
        assert percentiles == {p: None for p in percentiles}
        assert unrepresentable == ()


class TestClassifyDistributionSkipsIncoherentRatios:
    """A distribution derived from an incoherent ratio must not be published as sound."""

    def test_dominant_value_is_not_read_off_a_multi_entry_count_that_exceeds_non_null(
        self,
    ) -> None:
        counts = [40, 20]

        assert classify_distribution(counts, 5, exhaustive=True) == "uniform"

    def test_a_single_value_exhaustive_list_is_dominant_even_when_incoherent(self) -> None:
        """SPEC 2.2.7's single-value rule needs no denominator."""

        counts = [60]

        assert classify_distribution(counts, 5, exhaustive=True) == "dominant_value"

    def test_a_coherent_list_is_unaffected(self) -> None:
        counts = [96, 1]

        assert classify_distribution(counts, 100, exhaustive=True) == "dominant_value"
