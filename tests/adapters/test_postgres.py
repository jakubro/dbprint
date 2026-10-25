"""Postgres-specific adapter tests + DDL normalization unit tests.

The contract suite in `test_base_contract.py` covers the shared behaviour through
`adapter_factory`; what is left here is postgres-only. DDL normalization is unit-tested
against canned pg_dump output, no DB needed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, LiteralString, cast
from unittest.mock import patch

import pytest
import yaml

from dbprint.adapters import ColumnStats, StatisticsConfig, TableCounts, TableScope, pg_catalog
from dbprint.adapters import base as adapters_base
from dbprint.adapters.base import PhaseB
from dbprint.adapters.errors import QueryFailed
from dbprint.adapters.identifiers import Identity, UnknownTable
from dbprint.adapters.postgres import DIALECT, PostgresAdapter, PostgresConnectionError, introspect
from dbprint.adapters.postgres.connection import ConnectionParams, exec_query
from dbprint.adapters.postgres.ddl import extract_ddl, normalize
from dbprint.config import ProfileConfig, TablesConfig
from dbprint.config.project import RuleConfig, RuleMatch
from dbprint.spec.distribution import classify as classify_distribution
from tests._engine_run import conformance_errors
from tests._prints import connection_config
from tests.conftest import pg_connect


# Lazy-driver (missing [postgres] extra) unit test (no DB).


class TestMissingExtra:
    """Postgres adapter imports psycopg lazily; an absent driver gives an actionable error."""

    def test_connect_without_psycopg_raises_postgres_extra_hint(self) -> None:
        adapter = PostgresAdapter(
            {"host": "h", "port": "5432", "database": "d", "user": "u", "password": "p"},
        )

        with (
            patch(
                "dbprint.adapters.driver.importlib.import_module",
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
            introspect.columns(
                cast(Any, _RaisingConnection()),
                Identity.of(("d", "public", "t"), DIALECT),
            )

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


# Live-Postgres scenarios.


def _connected(credentials: dict[str, str]) -> PostgresAdapter:
    """A connected adapter that has enumerated - what per-table extraction requires.

    `pg_class` compares case-sensitively, so a table never enumerated has no spelling to bind.
    """

    adapter = PostgresAdapter(credentials)
    adapter.connect()
    adapter.list_tables(include=["*"], exclude=[])

    return adapter


@pytest.fixture
def fresh_postgres(postgres_test_db: dict[str, str]) -> Iterator[PostgresAdapter]:
    adapter = _connected(postgres_test_db)
    yield adapter
    adapter.close()


class TestLiveContract:
    def test_lists_seeded_tables(
        self,
        fresh_postgres: PostgresAdapter,
        postgres_test_db: dict[str, str],
    ) -> None:
        tables = fresh_postgres.list_tables(include=["*"], exclude=[])
        fqns = {t.fqn for t in tables}
        assert f"{postgres_test_db['database']}.seedbank.curator" in fqns
        assert f"{postgres_test_db['database']}.seedbank.herbarium" in fqns

    def test_skips_system_schemas(
        self,
        fresh_postgres: PostgresAdapter,
        postgres_test_db: dict[str, str],
    ) -> None:
        tables = fresh_postgres.list_tables(include=["*"], exclude=[])
        schemas = {t.namespace_path[1] for t in tables}

        assert "seedbank" in schemas
        assert not {s for s in schemas if s.startswith("pg_") or s == "information_schema"}

    def test_columns_include_all_seeded(
        self,
        fresh_postgres: PostgresAdapter,
        postgres_test_db: dict[str, str],
    ) -> None:
        cols = fresh_postgres.introspect_columns(f"{postgres_test_db['database']}.seedbank.curator")
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
        postgres_test_db: dict[str, str],
    ) -> None:
        rels = fresh_postgres.introspect_relationships(
            f"{postgres_test_db['database']}.seedbank.curator",
        )
        assert len(rels) == 1
        fk = rels[0]
        assert fk.column == ("herbarium_id",)
        assert fk.target_table == f"{postgres_test_db['database']}.seedbank.herbarium"
        assert fk.target_column == ("id",)
        assert fk.on_delete == "CASCADE"

    def test_index_excludes_pk_and_unique_backed(
        self,
        fresh_postgres: PostgresAdapter,
        postgres_test_db: dict[str, str],
    ) -> None:
        idxs = fresh_postgres.introspect_indexes(f"{postgres_test_db['database']}.seedbank.curator")
        names = {i.name for i in idxs}
        assert "curator_email_idx" in names

        for idx in idxs:
            assert not idx.name.endswith("_pkey")

    def test_comments_round_trip(
        self,
        fresh_postgres: PostgresAdapter,
        postgres_test_db: dict[str, str],
    ) -> None:
        comments = fresh_postgres.extract_comments(
            f"{postgres_test_db['database']}.seedbank.curator",
        )
        assert comments.table == "Primary curator table"
        assert comments.columns.get("email") == "user-facing email address"

    def test_ddl_extract_normalized(
        self,
        fresh_postgres: PostgresAdapter,
        postgres_test_db: dict[str, str],
    ) -> None:
        ddl = fresh_postgres.extract_ddl(f"{postgres_test_db['database']}.seedbank.curator")
        assert ddl.endswith("\n")
        assert "CREATE TABLE" in ddl
        assert "SET statement_timeout" not in ddl
        assert "PostgreSQL database dump" not in ddl

    def test_sample_values_returns_distinct_non_null(
        self,
        fresh_postgres: PostgresAdapter,
        postgres_test_db: dict[str, str],
    ) -> None:
        samples = fresh_postgres.sample_values(
            f"{postgres_test_db['database']}.seedbank.curator",
            "email",
            n=10,
        )
        assert len(set(samples)) == len(samples)
        assert all(s is not None for s in samples)


class TestPhysicalColumnIdentity:
    """SPEC 2.2.1: the `columns` map key is lowercase even where the catalog spelling differs."""

    def _seed_mixed_case(self, creds: dict[str, str]) -> None:

        with pg_connect(creds) as conn:
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
        adapter = _connected(postgres_test_db)

        try:
            cols = {
                c.name: c
                for c in adapter.introspect_columns(
                    f"{postgres_test_db['database']}.public.curator",
                )
            }
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
        adapter = _connected(postgres_test_db)

        try:
            indexes = adapter.introspect_indexes(f"{postgres_test_db['database']}.public.curator")
            comments = adapter.extract_comments(f"{postgres_test_db['database']}.public.curator")
        finally:
            adapter.close()

        assert any(idx.columns == ("fullname",) for idx in indexes)
        assert comments.columns.get("fullname") == "full legal name"

    def test_statistics_address_the_physical_column(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed_mixed_case(postgres_test_db)
        adapter = _connected(postgres_test_db)

        try:
            cols = adapter.introspect_columns(f"{postgres_test_db['database']}.public.curator")
            counts, stats = adapter.compute_statistics(
                f"{postgres_test_db['database']}.public.curator",
                cols,
                StatisticsConfig(),
                frozenset(),
            )
            samples = adapter.sample_values(
                f"{postgres_test_db['database']}.public.curator",
                "fullname",
                n=10,
            )
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

        from dbprint.config.project import RedactRule
        from dbprint.engine import Engine

        self._seed_mixed_case(postgres_test_db)
        conn = connection_config(
            output=tmp_path,
            tables=TablesConfig(include=(f"{postgres_test_db['database']}.public.curator",)),
            profile=ProfileConfig(
                redact=(RedactRule(sensitivity=("personal_name",), with_="mask"),),
            ),
        )
        adapter = PostgresAdapter(postgres_test_db)
        Engine(adapter, conn, tmp_path).generate()

        stats = yaml.safe_load(
            (
                tmp_path
                / "primary"
                / postgres_test_db["database"]
                / "public"
                / "curator"
                / "statistics.yaml"
            ).read_text(),
        )
        column = stats["columns"]["fullname"]

        assert column["physical_name"] == "fullName"
        assert column["inferred"]["sensitivity"] == "personal_name"
        assert column["redacted"] == "mask"

        errors = conformance_errors(tmp_path / "primary")
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )


class TestPhysicalTableIdentity:
    """SPEC 1.3: a quoted-created relation folds into the path, and is still addressable.

    Postgres stores a quoted identifier verbatim, so the folded path names an absent relation.
    """

    def _seed_mixed_case_table(self, creds: dict[str, str]) -> None:

        with pg_connect(creds) as conn:
            conn.execute('CREATE SCHEMA "Seedbank"')
            conn.execute('CREATE TABLE "Seedbank"."Accession" (id int, label text)')
            conn.execute(
                'INSERT INTO "Seedbank"."Accession" '
                "SELECT g, 'label-' || (g % 7) FROM generate_series(1, 120) g",
            )
            conn.execute('COMMENT ON TABLE "Seedbank"."Accession" IS \'the accession register\'')
            # reltuples stays -1 until analyzed, and an absent estimate would pass either way.
            conn.execute('ANALYZE "Seedbank"."Accession"')

    def test_both_segments_fold_into_the_path(self, postgres_test_db: dict[str, str]) -> None:
        self._seed_mixed_case_table(postgres_test_db)
        adapter = _connected(postgres_test_db)

        try:
            listed = adapter.list_tables(include=["*"], exclude=[])
        finally:
            adapter.close()

        entry = next(
            t for t in listed if t.fqn == f"{postgres_test_db['database']}.seedbank.accession"
        )
        assert entry.namespace_path == (postgres_test_db["database"], "seedbank", "accession")

    def test_the_catalog_reads_address_the_native_spelling(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed_mixed_case_table(postgres_test_db)
        adapter = _connected(postgres_test_db)

        try:
            cols = adapter.introspect_columns(f"{postgres_test_db['database']}.seedbank.accession")
            comments = adapter.extract_comments(
                f"{postgres_test_db['database']}.seedbank.accession",
            )
            estimate = adapter.estimate_row_count(
                f"{postgres_test_db['database']}.seedbank.accession",
            )
        finally:
            adapter.close()

        assert [c.name for c in cols] == ["id", "label"]
        assert comments.table == "the accession register"
        assert estimate == 120

    def test_the_statistics_read_the_real_rows(self, postgres_test_db: dict[str, str]) -> None:
        self._seed_mixed_case_table(postgres_test_db)
        adapter = _connected(postgres_test_db)

        try:
            cols = adapter.introspect_columns(f"{postgres_test_db['database']}.seedbank.accession")
            counts, stats = adapter.compute_statistics(
                f"{postgres_test_db['database']}.seedbank.accession",
                cols,
                StatisticsConfig(),
                frozenset(),
            )
            samples = adapter.sample_values(
                f"{postgres_test_db['database']}.seedbank.accession",
                "label",
                n=10,
            )
            sketch = adapter.compute_key_sketch(
                f"{postgres_test_db['database']}.seedbank.accession",
                "id",
                "integer",
                "integer",
                8,
            )
            normalized = adapter.compute_normalized_cardinality(
                f"{postgres_test_db['database']}.seedbank.accession",
                "label",
                next(c.classified_type for c in cols if c.name == "label"),
            )
        finally:
            adapter.close()

        assert counts.row_count == 120
        assert stats["id"].cardinality == 120
        assert samples, "sample_values addressed no rows on the quoted relation"
        assert len(sketch) == 8
        assert normalized == 7

    def test_ddl_extraction_finds_the_quoted_relation(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """pg_dump folds an unquoted `--table` pattern, so the pattern carries the quotes."""

        self._seed_mixed_case_table(postgres_test_db)
        adapter = _connected(postgres_test_db)

        try:
            ddl = adapter.extract_ddl(f"{postgres_test_db['database']}.seedbank.accession")
        finally:
            adapter.close()

        assert 'CREATE TABLE "Seedbank"."Accession"' in ddl

    def test_a_relation_that_was_never_enumerated_is_refused(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """Falling back to the folded path would read an absent relation as an empty one."""

        adapter = _connected(postgres_test_db)

        try:
            with pytest.raises(UnknownTable, match="call list_tables"):
                adapter.introspect_columns(f"{postgres_test_db['database']}.public.never_listed")
        finally:
            adapter.close()


class TestCollation:
    """SPEC 2.2.2/2.2.4: `cardinality` and its neighbors are collation-relative."""

    def _seed(self, creds: dict[str, str]) -> None:

        with pg_connect(creds) as conn:
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
        adapter = _connected(postgres_test_db)

        try:
            cols = {
                c.name: c
                for c in adapter.introspect_columns(f"{postgres_test_db['database']}.public.labels")
            }
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
        from dbprint.engine import Engine

        self._seed(postgres_test_db)
        conn = connection_config(
            output=tmp_path,
            tables=TablesConfig(include=(f"{postgres_test_db['database']}.public.labels",)),
        )
        adapter = PostgresAdapter(postgres_test_db)
        Engine(adapter, conn, tmp_path).generate()

        manifest = yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())
        stats = yaml.safe_load(
            (
                tmp_path
                / "primary"
                / postgres_test_db["database"]
                / "public"
                / "labels"
                / "statistics.yaml"
            ).read_text(),
        )
        columns = stats["columns"]

        assert manifest["default_collation"] and manifest["default_collation"] != "C"
        assert "collation" not in columns["plain"]
        assert columns["forced"]["collation"] == "C"

        errors = conformance_errors(tmp_path / "primary")
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )


class TestEdgeCases:
    def test_all_null_column(self, postgres_test_db: dict[str, str]) -> None:

        from dbprint.config import StatisticsConfig

        with pg_connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.nullable_t (id int PRIMARY KEY, opt text)")
            conn.execute("INSERT INTO public.nullable_t (id, opt) VALUES (1, NULL), (2, NULL)")

        adapter = _connected(postgres_test_db)

        try:
            cols = adapter.introspect_columns(f"{postgres_test_db['database']}.public.nullable_t")
            _, stats = adapter.compute_statistics(
                f"{postgres_test_db['database']}.public.nullable_t",
                cols,
                StatisticsConfig(),
                frozenset(),
            )
            assert stats["opt"].null_count == 2
            assert stats["opt"].null_rate == 1.0  # noqa: RUF069 - the expected value is an exact literal
            assert stats["opt"].cardinality == 0
        finally:
            adapter.close()

    def test_composite_fk_emits_array(self, postgres_test_db: dict[str, str]) -> None:

        with pg_connect(postgres_test_db) as conn:
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

        adapter = _connected(postgres_test_db)

        try:
            rels = adapter.introspect_relationships(f"{postgres_test_db['database']}.public.child")
            assert len(rels) == 1
            fk = rels[0]
            assert fk.column == ("x", "y")
            assert fk.target_column == ("a", "b")
            assert fk.target_table == f"{postgres_test_db['database']}.public.parent"
        finally:
            adapter.close()

    def test_self_referential_fk(self, postgres_test_db: dict[str, str]) -> None:

        with pg_connect(postgres_test_db) as conn:
            conn.execute(
                """
                CREATE TABLE public.curator (
                    id int PRIMARY KEY,
                    mentor_id int REFERENCES public.curator(id)
                )
                """,
            )

        adapter = _connected(postgres_test_db)

        try:
            rels = adapter.introspect_relationships(
                f"{postgres_test_db['database']}.public.curator",
            )
            assert len(rels) == 1
            assert rels[0].target_table == f"{postgres_test_db['database']}.public.curator"
            assert rels[0].column == ("mentor_id",)
            assert rels[0].target_column == ("id",)
        finally:
            adapter.close()

    def test_view_listed_but_no_relationships(self, postgres_test_db: dict[str, str]) -> None:

        with pg_connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.src (id int PRIMARY KEY)")
            conn.execute("CREATE VIEW public.src_v AS SELECT * FROM public.src")

        adapter = _connected(postgres_test_db)

        try:
            tables = {t.fqn: t for t in adapter.list_tables(include=["*"], exclude=[])}
            assert tables[f"{postgres_test_db['database']}.public.src_v"].type == "view"
        finally:
            adapter.close()

    def test_matview_listed_with_type(self, postgres_test_db: dict[str, str]) -> None:

        with pg_connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.src (id int PRIMARY KEY)")
            conn.execute("CREATE MATERIALIZED VIEW public.src_mv AS SELECT * FROM public.src")

        adapter = _connected(postgres_test_db)

        try:
            tables = {t.fqn: t for t in adapter.list_tables(include=["*"], exclude=[])}
            assert tables[f"{postgres_test_db['database']}.public.src_mv"].type == "matview"
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

        with pg_connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.mv_src (id int PRIMARY KEY, code text NOT NULL)")
            conn.execute(
                "CREATE MATERIALIZED VIEW public.mv_src_mv AS SELECT id, code FROM public.mv_src",
            )
            conn.execute("CREATE UNIQUE INDEX mv_src_mv_code_ux ON public.mv_src_mv (code)")

        adapter = _connected(postgres_test_db)

        try:
            groups = {
                g.columns
                for g in adapter.introspect_unique_keys(
                    f"{postgres_test_db['database']}.public.mv_src_mv",
                )
            }
            assert ("code",) in groups
        finally:
            adapter.close()

    def test_varied_types(self, postgres_test_db: dict[str, str]) -> None:

        from dbprint.config import StatisticsConfig

        with pg_connect(postgres_test_db) as conn:
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

        adapter = _connected(postgres_test_db)

        try:
            cols = adapter.introspect_columns(f"{postgres_test_db['database']}.public.varied")
            _, stats = adapter.compute_statistics(
                f"{postgres_test_db['database']}.public.varied",
                cols,
                StatisticsConfig(),
                frozenset(),
            )

            # bytea -> measured: counted, byte length, no value list (SPEC 2.2.3)
            assert stats["blob"].cardinality == 200
            assert stats["blob"].length is not None
            assert (stats["blob"].length.min, stats["blob"].length.max) == (2, 2)
            assert stats["blob"].values is None

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
            assert stats["id"].cardinality_ratio == 1.0  # noqa: RUF069 - the expected value is an exact literal
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

        from dbprint.config import StatisticsConfig

        with pg_connect(postgres_test_db) as conn:
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

        adapter = _connected(postgres_test_db)

        try:
            cols = adapter.introspect_columns(f"{postgres_test_db['database']}.public.field_site")
            _, stats = adapter.compute_statistics(
                f"{postgres_test_db['database']}.public.field_site",
                cols,
                StatisticsConfig(),
                frozenset(),
            )

            for name in ("addr", "postal_code"):
                assert stats[name].cardinality is None
                assert stats[name].cardinality_method is None

            # A scalar column on the same table measures normally.
            assert stats["label"].cardinality is not None
            assert stats["id"].cardinality is not None
        finally:
            adapter.close()

    def test_composite_bare_unique_index_preserves_column_order(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """The union with `pg_constraint` must not scramble a composite key's positions."""

        with pg_connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.pairs (id int PRIMARY KEY, b text NOT NULL, a text NOT NULL)",
            )
            conn.execute("CREATE UNIQUE INDEX pairs_b_a_ux ON public.pairs (b, a)")

        adapter = _connected(postgres_test_db)

        try:
            groups = [
                g.columns
                for g in adapter.introspect_unique_keys(
                    f"{postgres_test_db['database']}.public.pairs",
                )
            ]
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

        with pg_connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.soft_deletes (id int PRIMARY KEY, "
                "code text NOT NULL, deleted_at timestamp)",
            )
            conn.execute(
                "CREATE UNIQUE INDEX soft_deletes_code_ux ON public.soft_deletes (code) "
                "WHERE deleted_at IS NULL",
            )

        adapter = _connected(postgres_test_db)

        try:
            groups = {
                g.columns
                for g in adapter.introspect_unique_keys(
                    f"{postgres_test_db['database']}.public.soft_deletes",
                )
            }
            indexes = {
                i.columns: i
                for i in adapter.introspect_indexes(
                    f"{postgres_test_db['database']}.public.soft_deletes",
                )
            }
            assert ("code",) not in groups
            assert ("code",) in indexes
            assert indexes[("code",)].unique is True
        finally:
            adapter.close()


class TestExpressionAndIncludedMembers:
    """SPEC 2.2.12: a key's members are all columns, and an index lists its key columns only."""

    def _read(self, credentials: dict[str, str], ddl: str, table: str) -> tuple[list, list]:
        with pg_connect(credentials) as conn:
            for statement in ddl.strip().split(";\n"):
                conn.execute(cast(LiteralString, statement))

        adapter = _connected(credentials)
        fqn = f"{credentials['database']}.public.{table}"

        try:
            keys = [g.columns for g in adapter.introspect_unique_keys(fqn)]
            indexes = {i.name: i.columns for i in adapter.introspect_indexes(fqn)}
        finally:
            adapter.close()

        return keys, sorted(indexes.items())

    def test_a_unique_index_over_an_expression_declares_no_key(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        keys, indexes = self._read(
            postgres_test_db,
            """
            CREATE TABLE public.cultivar (code text NOT NULL, label text);
            CREATE UNIQUE INDEX cultivar_code_ux ON public.cultivar (lower(code))
            """,
            "cultivar",
        )

        assert (keys, indexes) == ([], [])

    def test_a_unique_index_mixing_a_column_and_an_expression_declares_no_key(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        keys, _ = self._read(
            postgres_test_db,
            """
            CREATE TABLE public.botanist (id int PRIMARY KEY, herbarium_id int, email text);
            CREATE UNIQUE INDEX botanist_email_ux ON public.botanist (herbarium_id, lower(email))
            """,
            "botanist",
        )

        assert keys == [("id",)]

    def test_an_included_column_is_never_part_of_a_key(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        keys, _ = self._read(
            postgres_test_db,
            """
            CREATE TABLE public.vault (id int NOT NULL, shelf text, code text, rack int);
            CREATE UNIQUE INDEX vault_id_ux ON public.vault (id) INCLUDE (shelf);
            ALTER TABLE public.vault ADD CONSTRAINT vault_code_uq UNIQUE (code) INCLUDE (rack)
            """,
            "vault",
        )

        assert sorted(keys) == [("code",), ("id",)]

    def test_a_plain_bare_unique_index_still_declares_its_key(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        keys, _ = self._read(
            postgres_test_db,
            """
            CREATE TABLE public.taxon (code text NOT NULL);
            CREATE UNIQUE INDEX taxon_code_ux ON public.taxon (code)
            """,
            "taxon",
        )

        assert keys == [("code",)]

    def test_a_secondary_index_lists_its_key_columns_only(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        _, indexes = self._read(
            postgres_test_db,
            """
            CREATE TABLE public.vault (id int PRIMARY KEY, rack int, shelf text);
            CREATE INDEX vault_rack_ix ON public.vault (rack) INCLUDE (shelf);
            CREATE INDEX vault_expr_ix ON public.vault ((rack + 1));
            CREATE INDEX vault_mixed_ix ON public.vault (shelf, (rack + 1))
            """,
            "vault",
        )

        assert indexes == [("vault_mixed_ix", ("shelf",)), ("vault_rack_ix", ("rack",))]

    def test_an_attnum_naming_no_live_column_is_refused_rather_than_dropped(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with pg_connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.seed_lot (id int, gone int, rank text)")
            conn.execute("ALTER TABLE public.seed_lot DROP COLUMN gone")
            row = conn.execute("SELECT 'public.seed_lot'::regclass::oid").fetchone()
            relid = int(row[0]) if row else 0

            with pytest.raises(ValueError, match=rf"attnums \[2, 9\] of relation {relid} "):
                pg_catalog.attnums_to_names(partial(exec_query, conn), relid, [1, 2, 3, 9])


class TestPhysicalLayout:
    """The declared partition key via `pg_get_partkeydef`, on the parent relation only."""

    def _connect(self, postgres_test_db: dict[str, str]) -> Any:

        return pg_connect(postgres_test_db)

    def test_a_single_column_key_is_recovered(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.curation_event (id int, logged_at date) PARTITION BY RANGE (logged_at)",
            )

        adapter = _connected(postgres_test_db)

        try:
            layout = adapter.introspect_physical_layout(
                f"{postgres_test_db['database']}.public.curation_event",
            )
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

        adapter = _connected(postgres_test_db)

        try:
            layout = adapter.introspect_physical_layout(
                f"{postgres_test_db['database']}.public.field_log",
            )
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

        adapter = _connected(postgres_test_db)

        try:
            layout = adapter.introspect_physical_layout(
                f"{postgres_test_db['database']}.public.logs",
            )
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

        adapter = _connected(postgres_test_db)

        try:
            layout = adapter.introspect_physical_layout(
                f"{postgres_test_db['database']}.public.buckets",
            )
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

        adapter = _connected(postgres_test_db)

        try:
            assert (
                adapter.introspect_physical_layout(f"{postgres_test_db['database']}.public.plain")
                is None
            )
        finally:
            adapter.close()

    def test_an_individual_partition_is_not_queried_for_its_own_key(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """Only the partitioned parent (`relkind = 'p'`) carries a key; a child inherits it.

        Read through `introspect`: `list_tables` excludes children, so the adapter refuses them.
        """

        with self._connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.specimen_loan (id int, logged_at date) PARTITION BY RANGE (logged_at)",
            )
            conn.execute(
                "CREATE TABLE public.specimen_loan_2024 PARTITION OF public.specimen_loan "
                "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
            )
            child = Identity.of(
                (postgres_test_db["database"], "public", "specimen_loan_2024"),
                DIALECT,
            )

            assert introspect.physical_layout(conn, child) is None


class TestViewDependencies:
    """`introspect_view_dependencies` via `pg_depend`/`pg_rewrite`, one query for the run."""

    def _connect(self, postgres_test_db: dict[str, str]) -> Any:

        return pg_connect(postgres_test_db)

    def test_a_view_reading_two_tables_lists_both(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.wide (id int)")
            conn.execute("CREATE TABLE public.narrow (id int)")
            conn.execute(
                "CREATE VIEW public.both_v AS "
                "SELECT w.id AS wide_id, n.id AS narrow_id FROM public.wide w, public.narrow n",
            )

        adapter = _connected(postgres_test_db)

        try:
            deps = adapter.introspect_view_dependencies()
            assert deps is not None
            assert set(deps[f"{postgres_test_db['database']}.public.both_v"]) == {
                f"{postgres_test_db['database']}.public.wide",
                f"{postgres_test_db['database']}.public.narrow",
            }
        finally:
            adapter.close()

    def test_a_view_over_a_view_names_the_view_not_its_own_source(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.wide (id int)")
            conn.execute("CREATE VIEW public.inner_v AS SELECT id FROM public.wide")
            conn.execute("CREATE VIEW public.outer_v AS SELECT id FROM public.inner_v")

        adapter = _connected(postgres_test_db)

        try:
            deps = adapter.introspect_view_dependencies()
            assert deps is not None
            assert deps[f"{postgres_test_db['database']}.public.outer_v"] == (
                f"{postgres_test_db['database']}.public.inner_v",
            )
            assert deps[f"{postgres_test_db['database']}.public.inner_v"] == (
                f"{postgres_test_db['database']}.public.wide",
            )
        finally:
            adapter.close()

    def test_a_view_reading_nothing_answers_with_an_empty_tuple(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A resolvable view with no object dependency still seeds an entry - `()`, not absent."""

        with self._connect(postgres_test_db) as conn:
            conn.execute("CREATE VIEW public.literal_v AS SELECT 1 AS x")

        adapter = _connected(postgres_test_db)

        try:
            deps = adapter.introspect_view_dependencies()
            assert deps is not None
            assert deps[f"{postgres_test_db['database']}.public.literal_v"] == ()
        finally:
            adapter.close()

    def test_a_materialized_view_is_named_the_same_way(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.wide (id int)")
            conn.execute("CREATE MATERIALIZED VIEW public.wide_mv AS SELECT id FROM public.wide")

        adapter = _connected(postgres_test_db)

        try:
            deps = adapter.introspect_view_dependencies()
            assert deps is not None
            assert deps[f"{postgres_test_db['database']}.public.wide_mv"] == (
                f"{postgres_test_db['database']}.public.wide",
            )
        finally:
            adapter.close()

    def test_a_plain_table_never_appears_as_a_key(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.plain (id int)")

        adapter = _connected(postgres_test_db)

        try:
            deps = adapter.introspect_view_dependencies()
            assert deps is not None
            assert f"{postgres_test_db['database']}.public.plain" not in deps
        finally:
            adapter.close()


class TestPartitionChildExclusion:
    """`list_tables` enumerates a partitioned table's parent, never its children."""

    def _connect(self, postgres_test_db: dict[str, str]) -> Any:

        return pg_connect(postgres_test_db)

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

        adapter = _connected(postgres_test_db)

        try:
            fqns = {t.fqn for t in adapter.list_tables(include=["*"], exclude=[])}
            assert f"{postgres_test_db['database']}.public.viability_check" in fqns
            assert f"{postgres_test_db['database']}.public.readings_2024" not in fqns
            assert f"{postgres_test_db['database']}.public.readings_2025" not in fqns
        finally:
            adapter.close()

    def test_a_detached_former_partition_enumerates_normally(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        with self._connect(postgres_test_db) as conn:
            conn.execute(
                "CREATE TABLE public.batch (id int, logged_at date) PARTITION BY RANGE (logged_at)",
            )
            conn.execute(
                "CREATE TABLE public.batch_2024 PARTITION OF public.batch "
                "FOR VALUES FROM ('2024-01-01') TO ('2025-01-01')",
            )
            conn.execute("ALTER TABLE public.batch DETACH PARTITION public.batch_2024")

        adapter = _connected(postgres_test_db)

        try:
            fqns = {t.fqn for t in adapter.list_tables(include=["*"], exclude=[])}
            assert f"{postgres_test_db['database']}.public.batch" in fqns
            assert f"{postgres_test_db['database']}.public.batch_2024" in fqns
        finally:
            adapter.close()


_PARTITIONED_FKS = """
CREATE TABLE public.herbarium (id int, rank text, PRIMARY KEY (id, rank)) PARTITION BY LIST (rank);
CREATE TABLE public.herbarium_us PARTITION OF public.herbarium FOR VALUES IN ('us');
CREATE TABLE public.herbarium_eu PARTITION OF public.herbarium FOR VALUES IN ('eu');
CREATE INDEX herbarium_rank_ix ON public.herbarium (rank);
CREATE TABLE public.viability_check (id int PRIMARY KEY, herbarium_id int, rank text,
  FOREIGN KEY (herbarium_id, rank) REFERENCES public.herbarium (id, rank));
CREATE TABLE public.field_round (id int, herbarium_id int, rank text, PRIMARY KEY (id, rank),
  FOREIGN KEY (herbarium_id, rank) REFERENCES public.herbarium (id, rank)) PARTITION BY LIST (rank);
CREATE TABLE public.field_round_us PARTITION OF public.field_round FOR VALUES IN ('us')
"""


class TestPartitionedForeignKeys:
    """A key into or out of a partitioned table is declared once; PostgreSQL's copies are not."""

    def _targets(self, credentials: dict[str, str], table: str, *statements: str) -> list[str]:
        with pg_connect(credentials) as conn:
            for statement in (*_PARTITIONED_FKS.strip().split(";\n"), *statements):
                conn.execute(cast(LiteralString, statement))

        adapter = _connected(credentials)

        try:
            fks = adapter.introspect_relationships(f"{credentials['database']}.public.{table}")
        finally:
            adapter.close()

        return [fk.target_table.rsplit(".", 1)[-1] for fk in fks]

    @pytest.mark.parametrize("referencer", ["viability_check", "field_round"])
    def test_one_edge_to_the_parent(
        self,
        postgres_test_db: dict[str, str],
        referencer: str,
    ) -> None:
        assert self._targets(postgres_test_db, referencer) == ["herbarium"]

    def test_a_partition_added_later_adds_no_edge(self, postgres_test_db: dict[str, str]) -> None:
        added = (
            "CREATE TABLE public.herbarium_ap PARTITION OF public.herbarium FOR VALUES IN ('ap')"
        )

        assert self._targets(postgres_test_db, "viability_check", added) == ["herbarium"]

    def test_a_detached_partition_publishes_its_own_key_once(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        detach = "ALTER TABLE public.field_round DETACH PARTITION public.field_round_us"

        assert self._targets(postgres_test_db, "field_round_us", detach) == ["herbarium"]

    def test_the_parent_reports_only_its_own_keys_and_indexes(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._targets(postgres_test_db, "viability_check")
        adapter = _connected(postgres_test_db)
        fqn = f"{postgres_test_db['database']}.public.herbarium"

        try:
            keys = [(k.columns, k.primary) for k in adapter.introspect_unique_keys(fqn)]
            indexes = [i.name for i in adapter.introspect_indexes(fqn)]
        finally:
            adapter.close()

        assert keys == [(("id", "rank"), True)]
        assert indexes == ["herbarium_rank_ix"]


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
            extract_ddl(params, Identity.of(("d", "public", "t"), DIALECT))

        # Read the record, not `caplog.text`: the installed formatter escapes the pattern's
        # own quotes under the full run and does not under a scoped one.
        message = next(r.getMessage() for r in caplog.records if "pg_dump" in r.getMessage())

        assert '--table="public"."t"' in message
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


class TestHashOrderedDraw:
    """SPEC 4.1.2: the distinct draw is ordered by a hash of the value, not storage order."""

    def test_a_value_inserted_last_is_still_reachable(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """5,000 rows stays on the small path, where n=1000 clears `n * SMALL_TABLE_FACTOR`."""

        with pg_connect(postgres_test_db) as conn:
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

        adapter = _connected(postgres_test_db)

        try:
            values = adapter.sample_values(
                f"{postgres_test_db['database']}.public.late_shape",
                "v",
                n=1000,
            )
            late = [v for v in values if str(v).startswith("11111111-1111-4111-8111-")]

            assert late, "the draw never reached a value inserted after the first n rows"
        finally:
            adapter.close()

    def test_two_draws_over_unchanged_data_agree(self, postgres_test_db: dict[str, str]) -> None:
        """The hash order is a function of the table's own seed, not session state."""

        with pg_connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.stable_draw (v text)")
            conn.execute(
                "INSERT INTO public.stable_draw SELECT 'val-' || i FROM generate_series(1, 5000) i",
            )

        adapter = _connected(postgres_test_db)

        try:
            first = adapter.sample_values(
                f"{postgres_test_db['database']}.public.stable_draw",
                "v",
                n=50,
            )
            second = adapter.sample_values(
                f"{postgres_test_db['database']}.public.stable_draw",
                "v",
                n=50,
            )

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
    def _profile(creds: dict[str, str], fqn: str, threshold: int) -> PhaseB:
        from dbprint.config import StatisticsConfig

        adapter = _connected(creds)

        try:
            cols = adapter.introspect_columns(fqn)

            with patch.object(adapters_base, "APPROXIMATE_THRESHOLD", threshold):
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

        with pg_connect(creds) as conn:
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
        stats = self._profile(
            postgres_test_db,
            f"{postgres_test_db['database']}.public.wide_t",
            threshold=10,
        )

        # Every value distinct: the estimate must land on the row count, not zero, and a
        # ratio of 1.0 reaches the near-unique re-probe, which counts it exactly.
        assert stats["id"].cardinality == 500
        assert stats["id"].cardinality_ratio == 1.0  # noqa: RUF069 - the expected value is an exact literal
        assert stats["id"].cardinality_method == "exact"

        # A small enumeration stays under the re-probe ratio and keeps its positive estimate.
        assert stats["label"].cardinality == 4
        assert stats["label"].cardinality_method == "approximate"

    def test_estimated_column_does_not_take_the_enumeration_branch(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A near-unique numeric column keeps its bounds and its top-N slice, never a full
        categorical enumeration of its (near-)500 distinct values.
        """

        self._seed(postgres_test_db, analyze=True)
        stats = self._profile(
            postgres_test_db,
            f"{postgres_test_db['database']}.public.wide_t",
            threshold=10,
        )

        assert stats["id"].range is not None
        assert (stats["id"].cardinality_ratio or 0) >= 0.9999
        assert stats["id"].values is not None
        assert len(stats["id"].values) <= StatisticsConfig().top_n_values, (
            "numeric takes the top-N branch, never the categorical enumeration (SPEC 2.2.3)"
        )

    def test_unanalyzed_column_falls_back_to_the_exact_count(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """No planner row is the absence of an estimate, not a zero."""

        self._seed(postgres_test_db, analyze=False)
        stats = self._profile(
            postgres_test_db,
            f"{postgres_test_db['database']}.public.wide_t",
            threshold=10,
        )

        assert stats["id"].cardinality == 500
        assert stats["id"].cardinality_method == "exact"

    def test_below_the_threshold_reports_exact(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._seed(postgres_test_db, analyze=True)
        stats = self._profile(
            postgres_test_db,
            f"{postgres_test_db['database']}.public.wide_t",
            threshold=100_000,
        )

        assert stats["id"].cardinality == 500
        assert stats["id"].cardinality_method == "exact"


class TestInheritanceParentStatistics:
    """A parent's statements read every descendant, so its catalog figures describe them too."""

    @staticmethod
    def _run(creds: dict[str, str], *statements: str) -> None:
        with pg_connect(creds) as conn:
            conn.execute("CREATE SCHEMA IF NOT EXISTS seedbank")

            for statement in statements:
                conn.execute(cast(LiteralString, statement))

    @staticmethod
    def _read(creds: dict[str, str], table: str) -> tuple[int | None, int | None, str | None]:
        from dbprint.config import StatisticsConfig

        fqn = f"{creds['database']}.seedbank.{table}"
        adapter = _connected(creds)

        try:
            estimate = adapter.estimate_row_count(fqn)

            with patch.object(adapters_base, "APPROXIMATE_THRESHOLD", 10):
                _, computed = adapter.compute_statistics(
                    fqn,
                    adapter.introspect_columns(fqn),
                    StatisticsConfig(),
                    frozenset(),
                )
        finally:
            adapter.close()

        return estimate, computed["rank"].cardinality, computed["rank"].cardinality_method

    _TREE = (
        "CREATE TABLE seedbank.field_log (id int NOT NULL, rank text NOT NULL)",
        "CREATE TABLE seedbank.field_round () INHERITS (seedbank.field_log)",
        "CREATE TABLE seedbank.field_site () INHERITS (seedbank.field_round)",
        "INSERT INTO seedbank.field_log SELECT g, 'r' || (g % 10) FROM generate_series(1, 300) g",
        (
            "INSERT INTO seedbank.field_round "
            "SELECT g, 'round_' || (g % 100) FROM generate_series(301, 600) g"
        ),
        (
            "INSERT INTO seedbank.field_site "
            "SELECT g, 'site_' || (g % 5) FROM generate_series(601, 700) g"
        ),
        "ANALYZE seedbank.field_log",
        "ANALYZE seedbank.field_round",
        "ANALYZE seedbank.field_site",
    )

    def test_a_parent_reads_the_inherited_estimates_of_its_whole_tree(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._run(postgres_test_db, *self._TREE)

        assert self._read(postgres_test_db, "field_log") == (700, 115, "approximate")

    def test_an_empty_child_never_analyzed_counts_nothing(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._run(
            postgres_test_db,
            *self._TREE,
            "CREATE TABLE seedbank.seed_lot () INHERITS (seedbank.field_log) "
            "WITH (autovacuum_enabled = false)",
        )

        assert self._read(postgres_test_db, "field_log") == (700, 115, "approximate")

    def test_a_loaded_child_never_analyzed_leaves_no_estimate(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._run(
            postgres_test_db,
            "CREATE TABLE seedbank.fieldwork (id int NOT NULL, rank text NOT NULL)",
            "INSERT INTO seedbank.fieldwork SELECT g, 'r' || (g % 10) FROM generate_series(1, 300) g",
            "ANALYZE seedbank.fieldwork",
            "CREATE TABLE seedbank.fieldwork_backlog () INHERITS (seedbank.fieldwork) "
            "WITH (autovacuum_enabled = false)",
            "INSERT INTO seedbank.fieldwork_backlog "
            "SELECT g, 'backlog_' || (g % 100) FROM generate_series(301, 600) g",
        )

        assert self._read(postgres_test_db, "fieldwork") == (None, 110, "exact")

    def test_a_parent_analyzed_before_its_child_existed_is_counted(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._run(
            postgres_test_db,
            "CREATE TABLE seedbank.orchard (id int NOT NULL, rank text NOT NULL)",
            "INSERT INTO seedbank.orchard SELECT g, 'r' || (g % 10) FROM generate_series(1, 300) g",
            "ANALYZE seedbank.orchard",
            "CREATE TABLE seedbank.orchard_row () INHERITS (seedbank.orchard)",
            "INSERT INTO seedbank.orchard_row "
            "SELECT g, 'row_' || (g % 100) FROM generate_series(301, 600) g",
            "ANALYZE seedbank.orchard_row",
        )

        assert self._read(postgres_test_db, "orchard") == (600, 110, "exact")

    def test_a_parent_whose_last_child_was_dropped_reads_its_own_row(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._run(
            postgres_test_db,
            "CREATE TABLE seedbank.nursery (id int NOT NULL, rank text NOT NULL)",
            "CREATE TABLE seedbank.nursery_bed () INHERITS (seedbank.nursery)",
            "INSERT INTO seedbank.nursery SELECT g, 'r' || (g % 10) FROM generate_series(1, 300) g",
            "INSERT INTO seedbank.nursery_bed "
            "SELECT g, 'bed_' || (g % 100) FROM generate_series(301, 600) g",
            "ANALYZE seedbank.nursery",
            "DROP TABLE seedbank.nursery_bed",
        )

        assert self._read(postgres_test_db, "nursery") == (300, 10, "approximate")

    def test_a_plain_table_reads_its_own_figures(self, postgres_test_db: dict[str, str]) -> None:
        self._run(
            postgres_test_db,
            "CREATE TABLE seedbank.batch (id int NOT NULL, rank text NOT NULL)",
            "INSERT INTO seedbank.batch SELECT g, 'r' || (g % 10) FROM generate_series(1, 300) g",
            "ANALYZE seedbank.batch",
        )

        assert self._read(postgres_test_db, "batch") == (300, 10, "approximate")

    def test_a_partitioned_table_reads_its_own_figures_once_analyzed(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        self._run(
            postgres_test_db,
            "CREATE TABLE seedbank.sowing_trial (id int NOT NULL, rank text NOT NULL) "
            "PARTITION BY RANGE (id)",
            "CREATE TABLE seedbank.sowing_trial_early PARTITION OF seedbank.sowing_trial "
            "FOR VALUES FROM (1) TO (301)",
            "CREATE TABLE seedbank.sowing_trial_late PARTITION OF seedbank.sowing_trial "
            "FOR VALUES FROM (301) TO (1000)",
            "INSERT INTO seedbank.sowing_trial "
            "SELECT g, 'r' || (g % 7) FROM generate_series(1, 600) g",
            "ANALYZE seedbank.sowing_trial_early",
            "ANALYZE seedbank.sowing_trial_late",
        )

        assert self._read(postgres_test_db, "sowing_trial") == (None, 7, "exact")

        self._run(postgres_test_db, "ANALYZE seedbank.sowing_trial")

        assert self._read(postgres_test_db, "sowing_trial") == (600, 7, "approximate")


class TestStaleEstimateCannotUnboundTheRead:
    """A wrong estimate must not turn into an unbounded enumeration.

    An estimate under `enumeration_threshold` while the truth is far above still routes the
    column into the categorical branch, so the query, not the estimate, has to bound it. A
    capped list looks the same either way, so these tests assert on the statement.
    """

    @staticmethod
    def _seed(creds: dict[str, str]) -> None:
        """Analyze on four labels, then add two thousand more without re-analyzing."""

        with pg_connect(creds) as conn:
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
    def _profile(creds: dict[str, str]) -> tuple[PhaseB, list[str]]:
        from dbprint.config import StatisticsConfig
        from tests.adapters._dialects import install_recorder

        adapter = _connected(creds)
        recorder = install_recorder(adapter)

        try:
            cols = adapter.introspect_columns(f"{creds['database']}.public.stale_t")

            with patch.object(adapters_base, "APPROXIMATE_THRESHOLD", 10):
                _, computed = adapter.compute_statistics(
                    f"{creds['database']}.public.stale_t",
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

        with pg_connect(creds) as conn:
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
        from dbprint.config import StatisticsConfig

        adapter = _connected(creds)

        try:
            cols = adapter.introspect_columns(f"{creds['database']}.public.null_heavy_t")

            with patch.object(adapters_base, "APPROXIMATE_THRESHOLD", 10):
                _, computed = adapter.compute_statistics(
                    f"{creds['database']}.public.null_heavy_t",
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

        with pg_connect(creds) as conn:
            conn.execute("CREATE TABLE public.scoped_t (id int, bucket int)")
            conn.execute(
                "INSERT INTO public.scoped_t SELECT g, g % 4 FROM generate_series(1, 400) g",
            )

            # reltuples is -1 until analyzed; without this every draw takes the direct read.
            if analyze:
                conn.execute("ANALYZE public.scoped_t")

    @staticmethod
    def _sample(creds: dict[str, str], n: int, scope: TableScope | None) -> list:
        adapter = _connected(creds)

        try:
            return adapter.sample_values(f"{creds['database']}.public.scoped_t", "id", n, scope)
        finally:
            adapter.close()

    @staticmethod
    def _profile(creds: dict[str, str], scope: TableScope | None) -> tuple[TableCounts, PhaseB]:
        from dbprint.config import StatisticsConfig

        adapter = _connected(creds)

        try:
            cols = adapter.introspect_columns(f"{creds['database']}.public.scoped_t")

            return adapter.compute_statistics(
                f"{creds['database']}.public.scoped_t",
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
        assert stats["id"].cardinality_ratio == 1.0  # noqa: RUF069 - the expected value is an exact literal

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
        from tests.adapters._dialects import install_recorder

        adapter = _connected(creds)
        recorder = install_recorder(adapter)

        try:
            adapter.sample_values(f"{creds['database']}.public.scoped_t", "bucket", n, scope)

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
    def _profile(creds: dict[str, str]) -> PhaseB:

        from dbprint.config import StatisticsConfig

        with pg_connect(creds) as conn:
            conn.execute("CREATE TABLE public.field_round (id int, run_at time)")
            # 60 distinct values clear the enumeration threshold, so the column is temporal.
            conn.execute(
                "INSERT INTO public.field_round "
                "SELECT g, TIME '08:00:00' + (g % 60) * INTERVAL '1 minute' "
                "FROM generate_series(1, 200) g",
            )

        adapter = _connected(creds)

        try:
            cols = adapter.introspect_columns(f"{creds['database']}.public.field_round")
            _, stats = adapter.compute_statistics(
                f"{creds['database']}.public.field_round",
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

        with pg_connect(creds) as conn:
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
    def _profile(creds: dict[str, str]) -> PhaseB:
        from dbprint.config import StatisticsConfig

        adapter = _connected(creds)

        try:
            cols = adapter.introspect_columns(f"{creds['database']}.public.viability_check")
            _, stats = adapter.compute_statistics(
                f"{creds['database']}.public.viability_check",
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
        from dbprint.adapters.postgres import stats as pg_stats

        self._seed(postgres_test_db, [])
        utc_rng, shifted_rng = _taken_at_in_two_zones(
            postgres_test_db,
            pg_stats._fetch_temporal_block,
            60,
        )

        assert shifted_rng.min == utc_rng.min
        assert shifted_rng.max == utc_rng.max

    def test_value_list_rendering_is_independent_of_session_timezone(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """A low-cardinality timestamptz's `values` must agree with `range` on the frame."""

        from dbprint.adapters.postgres import stats as pg_stats

        self._seed(postgres_test_db, [])
        utc_values, shifted_values = _taken_at_in_two_zones(
            postgres_test_db,
            pg_stats._fetch_value_list,
            240,
        )

        assert shifted_values == utc_values
        assert all(entry.value.endswith("Z") for entry in utc_values)

    def test_empty_non_null_set_does_not_clamp_to_a_bogus_span(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        """`GREATEST`/`LEAST` ignore a NULL argument rather than propagating it.

        Called directly rather than through `compute_statistics`: an all-NULL column has
        cardinality 0, so the classifier never routes it to the temporal branch.
        """

        from dbprint.adapters.base import ColumnMeta
        from dbprint.adapters.postgres import stats as pg_stats
        from dbprint.config import StatisticsConfig

        with pg_connect(postgres_test_db) as conn:
            conn.execute("CREATE TABLE public.all_null_ts (id int, taken_at timestamptz)")
            conn.execute("INSERT INTO public.all_null_ts (id, taken_at) VALUES (1, NULL)")

            col = ColumnMeta(
                name="taken_at",
                sql_type="timestamp with time zone",
                nullable=True,
                default=None,
                ordinal=1,
            )
            rng, percentiles, _, unrepresentable, _, _, _ = pg_stats._fetch_temporal_block(
                conn,
                pg_stats.source(f"{postgres_test_db['database']}.public.all_null_ts", None),
                col,
                0,
                StatisticsConfig(),
            )

        assert rng.min is None
        assert rng.max is None
        assert rng.span_days == 0
        assert percentiles == dict.fromkeys(percentiles)
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


def _taken_at_in_two_zones(
    creds: dict[str, str],
    fetch: Callable[..., Any],
    limit: int,
) -> tuple[Any, Any]:

    from dbprint.adapters.base import ColumnMeta
    from dbprint.adapters.postgres import stats as pg_stats
    from dbprint.config import StatisticsConfig

    col = ColumnMeta(
        name="taken_at",
        sql_type="timestamp with time zone",
        nullable=True,
        default=None,
        ordinal=1,
    )
    source = pg_stats.source(f"{creds['database']}.public.viability_check", None)
    read = []

    with pg_connect(creds) as conn:
        for zone in ("UTC", "America/New_York"):
            conn.execute(f"SET TimeZone = '{zone}'")
            first, *_ = fetch(conn, source, col, limit, StatisticsConfig())
            read.append(first)

    return read[0], read[1]


class TestPercentInReadText:
    """A `%` in a column, a filter or a JSON key reaches psycopg in a statement that binds nothing."""

    @staticmethod
    def _seed(creds: dict[str, str]) -> None:
        with pg_connect(creds) as conn:
            conn.execute(
                'CREATE TABLE public.germination_trial ("germinated%" text, reading jsonb)',
            )
            conn.execute(
                "INSERT INTO public.germination_trial "
                "SELECT (ARRAY['low', 'mid', 'high'])[g % 3 + 1], "
                "jsonb_build_object('moisture%', (ARRAY['dry', 'damp', 'wet'])[g % 3 + 1]) "
                "FROM generate_series(1, 60) g",
            )
            conn.execute("CREATE TABLE public.curator (surname text)")
            conn.execute(
                "INSERT INTO public.curator "
                "SELECT (ARRAY['Linden', 'Larch', 'Rowan'])[g % 3 + 1] FROM generate_series(1, 60) g",
            )
            conn.execute("CREATE TABLE public.field_round (id int, site text)")
            conn.execute(
                "INSERT INTO public.field_round "
                "SELECT g, (ARRAY['north', 'south'])[g % 2 + 1] FROM generate_series(1, 60) g",
            )

    @staticmethod
    def _filtered(fqn: str, predicate: str) -> RuleConfig:
        return RuleConfig(
            match=RuleMatch(tables=(fqn,)),
            profile={"read": {"tables": {"mode": "filter", "filter": predicate}}},
        )

    def test_every_column_key_and_filtered_table_is_measured(
        self,
        postgres_test_db: dict[str, str],
        tmp_path: Path,
    ) -> None:
        from dbprint.engine import Engine

        self._seed(postgres_test_db)
        database = postgres_test_db["database"]
        conn = replace(
            connection_config(
                output=tmp_path,
                tables=TablesConfig(include=(f"{database}.public.*",)),
            ),
            rules=(
                self._filtered(f"{database}.public.curator", "surname LIKE 'L%'"),
                self._filtered(f"{database}.public.field_round", "id % 2 = 0"),
            ),
        )
        Engine(PostgresAdapter(postgres_test_db), conn, tmp_path).generate()
        root = tmp_path / "primary" / database / "public"

        def columns(table: str) -> dict[str, Any]:
            return yaml.safe_load((root / table / "statistics.yaml").read_text())["columns"]

        trial = columns("germination_trial")
        part = trial["reading"]["parts"]['["moisture%"]']
        round_ = columns("field_round")

        assert {v["value"] for v in trial["germinated%"]["values"]} == {"low", "mid", "high"}
        assert "unmeasured" not in trial["germinated%"]
        assert {v["value"] for v in part["values"]} == {"dry", "damp", "wet"}
        assert {v["value"] for v in columns("curator")["surname"]["values"]} == {"Linden", "Larch"}
        assert round_["site"]["values"]
        assert "unmeasured" not in round_["id"]
        assert round_["site"]["rows_scanned"] == 30
