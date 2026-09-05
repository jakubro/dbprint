"""Redshift-specific behaviors the contract suite's default fixtures do not exercise - run
against Postgres wrapped in `RedshiftDialectShim`; there is no local Redshift substrate.
"""

from __future__ import annotations

import secrets

import psycopg
import pytest
from psycopg import sql

from dbprint.adapters import RedshiftAdapter, StatisticsConfig
from dbprint.adapters.redshift.adapter import UnknownTable
from dbprint.spec.sketch import low64_md5
from tests.adapters.conftest import RedshiftDialectShim
from tests.adapters.test_dialect_guard import _install_recorder
from tests.conftest import PostgresCluster


def _redshift_adapter(shim: RedshiftDialectShim) -> RedshiftAdapter:
    """A connected adapter that has enumerated - what per-table extraction requires.

    The catalog stores a quoted `CREATE`'s case, so a relation never enumerated has no spelling.
    """

    adapter = RedshiftAdapter(
        {"host": "redshift", "database": "seedbank", "user": "test", "password": "test"},
        cursor_factory=lambda _params: shim,
    )
    adapter.connect()
    adapter.list_tables(include=["*"], exclude=[])

    return adapter


@pytest.fixture
def redshift_scratch_db(postgres_cluster: PostgresCluster):
    """A bare Postgres database (no contract schema) for tests that build their own DDL."""

    db_name = f"rs_scratch_{secrets.token_hex(4)}"
    admin_creds = {
        "host": "127.0.0.1",
        "port": str(postgres_cluster.port),
        "database": "postgres",
        "user": postgres_cluster.superuser,
        "password": "",
    }
    conn = psycopg.connect(
        host=admin_creds["host"],
        port=int(admin_creds["port"]),
        dbname=admin_creds["database"],
        user=admin_creds["user"],
        password=admin_creds["password"],
        autocommit=True,
    )
    conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
    conn.close()

    db_conn = psycopg.connect(
        host=admin_creds["host"],
        port=int(admin_creds["port"]),
        dbname=db_name,
        user=admin_creds["user"],
        password=admin_creds["password"],
        autocommit=True,
    )

    try:
        yield db_conn
    finally:
        db_conn.close()
        cleanup = psycopg.connect(
            host=admin_creds["host"],
            port=int(admin_creds["port"]),
            dbname=admin_creds["database"],
            user=admin_creds["user"],
            password=admin_creds["password"],
            autocommit=True,
        )
        cleanup.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db_name)),
        )
        cleanup.close()


class TestPhysicalTableIdentity:
    """SPEC 1.3: a quoted-created relation folds into the path, and is still addressable.

    The catalog stores a quoted `CREATE`'s case, so the folded path names an absent relation.
    """

    @staticmethod
    def _seed(conn) -> None:
        conn.execute('CREATE SCHEMA "Seedbank"')
        conn.execute('CREATE TABLE "Seedbank"."Accession" (id int, label varchar(32))')
        conn.execute(
            'INSERT INTO "Seedbank"."Accession" '
            "SELECT g, 'label-' || (g % 7) FROM generate_series(1, 120) g",
        )

    def test_both_segments_fold_into_the_path(self, redshift_scratch_db) -> None:
        self._seed(redshift_scratch_db)
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            listed = adapter.list_tables(include=["*"], exclude=[])
        finally:
            adapter.close()

        entry = next(t for t in listed if t.fqn == "seedbank.accession")
        assert entry.namespace_path == ("seedbank", "accession")

    def test_every_statement_carries_the_catalog_spelling(self, redshift_scratch_db) -> None:
        self._seed(redshift_scratch_db)
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))
        recorder = _install_recorder(adapter)

        try:
            cols = adapter.introspect_columns("seedbank.accession")
            counts, stats = adapter.compute_statistics(
                "seedbank.accession",
                cols,
                StatisticsConfig(),
                frozenset(),
            )
            samples = adapter.sample_values("seedbank.accession", "label", n=10)
        finally:
            adapter.close()

        assert [c.name for c in cols] == ["id", "label"]
        assert counts.row_count == 120
        assert stats["id"].cardinality == 120
        assert samples, "sample_values addressed no rows on the quoted relation"

        # `flattened()` case-folds, which is exactly the distinction under test here.
        statements = recorder.statements
        assert any('"Seedbank"."Accession"' in s for s in statements)
        assert not any('"seedbank"."accession"' in s for s in statements)
        assert ("Seedbank", "Accession") in [tuple(p) for _s, p in recorder.bound]

    def test_ddl_extraction_shows_the_quoted_relation(self, redshift_scratch_db) -> None:
        """`SHOW TABLE` quoting is case-significant, so the folded name would find nothing."""

        self._seed(redshift_scratch_db)
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))
        recorder = _install_recorder(adapter)

        try:
            adapter.extract_ddl("seedbank.accession")
        finally:
            adapter.close()

        show = next(s for s in recorder.statements if s.lower().startswith("show table"))
        assert show.strip() == 'SHOW TABLE "Seedbank"."Accession"'

    def test_a_relation_that_was_never_enumerated_is_refused(self, redshift_scratch_db) -> None:
        """Falling back to the folded path would read an absent relation as an empty one."""

        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            with pytest.raises(UnknownTable, match="call list_tables"):
                adapter.introspect_columns("public.never_listed")
        finally:
            adapter.close()


class TestPhysicalLayout:
    """SVV_REDSHIFT_COLUMNS.sortkey: interleaved keys encode as alternating signs."""

    def test_an_interleaved_sortkey_is_not_read_as_absent(self, redshift_scratch_db) -> None:
        """Alternating signs (-1, 2, -3, ...) must not fail a per-column `< 0` test."""

        redshift_scratch_db.execute(
            "CREATE TABLE public.hedge (vault_id int, logged_at timestamp, note text)",
        )
        shim = RedshiftDialectShim(
            redshift_scratch_db,
            sortkey_by_table={"public.hedge": (("vault_id", -1), ("logged_at", 1))},
        )
        adapter = _redshift_adapter(shim)

        try:
            layout = adapter.introspect_physical_layout("public.hedge")
        finally:
            adapter.close()

        assert layout is not None
        assert layout.mechanism == "sort"
        assert [k.column for k in layout.keys] == ["vault_id", "logged_at"]

    def test_a_compound_sortkey_preserves_declaration_order(self, redshift_scratch_db) -> None:
        redshift_scratch_db.execute(
            "CREATE TABLE public.compound (a int, b int, c int)",
        )
        shim = RedshiftDialectShim(
            redshift_scratch_db,
            sortkey_by_table={"public.compound": (("a", 1), ("b", 1), ("c", 1))},
        )
        adapter = _redshift_adapter(shim)

        try:
            layout = adapter.introspect_physical_layout("public.compound")
        finally:
            adapter.close()

        assert layout is not None
        assert [k.column for k in layout.keys] == ["a", "b", "c"]

    def test_no_declared_sortkey_is_absent_not_empty(self, redshift_scratch_db) -> None:
        redshift_scratch_db.execute("CREATE TABLE public.plain (a int)")
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            layout = adapter.introspect_physical_layout("public.plain")
        finally:
            adapter.close()

        assert layout is None


class TestKeySketch:
    """SPEC 2.2.14's low64_md5, ported through STRTOL's two-32-bit-halves recombination."""

    def test_agrees_with_the_shared_spec_definition_on_a_wide_sample(
        self,
        redshift_scratch_db,
    ) -> None:
        """Every adapter's in-database hash MUST reproduce `spec.sketch.low64_md5` exactly."""

        redshift_scratch_db.execute("CREATE TABLE public.hashed (v varchar(64))")
        values = [f"value-{i}" for i in range(200)]
        rows = ", ".join(f"('{v}')" for v in values)
        redshift_scratch_db.execute(f"INSERT INTO public.hashed (v) VALUES {rows}")

        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            sketch = adapter.compute_key_sketch("public.hashed", "v", "varchar", "text", k=1000)
        finally:
            adapter.close()

        expected = sorted(low64_md5(v) for v in values)

        assert list(sketch) == expected

    def test_every_value_is_a_full_unsigned_64_bit_integer(self, redshift_scratch_db) -> None:
        """A value whose top bit is set must not sort negative or overflow STRTOL's range."""

        redshift_scratch_db.execute("CREATE TABLE public.hashed2 (v varchar(64))")
        values = [f"probe-{i}" for i in range(500)]
        rows = ", ".join(f"('{v}')" for v in values)
        redshift_scratch_db.execute(f"INSERT INTO public.hashed2 (v) VALUES {rows}")

        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            sketch = adapter.compute_key_sketch("public.hashed2", "v", "varchar", "text", k=1000)
        finally:
            adapter.close()

        assert sketch, "the probe table seeded no distinct values"
        assert all(0 <= h < 2**64 for h in sketch)
        # 500 draws make a top-bit-set value near-certain - a sketch built only from small
        # values would still pass a regression to the wrong MD5 half or dropped high 32 bits.
        assert any(h >= 2**63 for h in sketch), "no sampled hash exercised the top bit"


class TestDefaultCollation:
    def test_reports_the_two_valued_model(self, redshift_scratch_db) -> None:
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            collation = adapter.default_collation()
        finally:
            adapter.close()

        assert collation == "case_sensitive"


class TestViewDependencies:
    """A late-binding view has no `pg_rewrite` entry at all - the producer could not ask,
    so the key must be omitted, never published as `[]`.
    """

    def test_a_late_binding_view_omits_depends_on(self, redshift_scratch_db) -> None:
        redshift_scratch_db.execute("CREATE TABLE public.source_table (id int)")
        redshift_scratch_db.execute(
            "CREATE VIEW public.late_view AS SELECT id FROM public.source_table",
        )
        shim = RedshiftDialectShim(
            redshift_scratch_db,
            late_binding_views=frozenset({"public.late_view"}),
        )
        adapter = _redshift_adapter(shim)

        try:
            deps = adapter.introspect_view_dependencies()
        finally:
            adapter.close()

        assert deps is not None
        assert "public.late_view" not in deps

    def test_an_ordinary_view_still_lists_its_source(self, redshift_scratch_db) -> None:
        redshift_scratch_db.execute("CREATE TABLE public.source_table (id int)")
        redshift_scratch_db.execute(
            "CREATE VIEW public.ordinary_view AS SELECT id FROM public.source_table",
        )
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            deps = adapter.introspect_view_dependencies()
        finally:
            adapter.close()

        assert deps is not None
        assert deps["public.ordinary_view"] == ("public.source_table",)


class TestRelationshipsViaPgConstraint:
    """`SHOW CONSTRAINTS`'s FOREIGN KEYS form shares no column shape with its PRIMARY KEYS form -
    reading `pg_constraint` directly needs neither.
    """

    def test_a_composite_foreign_key_reports_its_real_arity(self, redshift_scratch_db) -> None:
        redshift_scratch_db.execute(
            "CREATE TABLE public.ref_parent (x int, y varchar(8), PRIMARY KEY (x, y))",
        )
        redshift_scratch_db.execute(
            "CREATE TABLE public.fk_child (a int, b varchar(8), "
            "FOREIGN KEY (a, b) REFERENCES public.ref_parent (x, y))",
        )
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            edges = adapter.introspect_relationships("public.fk_child")
        finally:
            adapter.close()

        assert len(edges) == 1, f"expected exactly one composite FK, got {len(edges)}: {edges}"
        edge = edges[0]
        assert edge.column == ("a", "b"), f"source columns duplicated or reordered: {edge.column}"
        assert edge.target_table == "public.ref_parent"
        assert edge.target_column == ("x", "y")
        assert edge.on_delete == "NO ACTION"
        assert edge.on_update == "NO ACTION"


class TestUniqueKeysViaPgConstraint:
    """`SHOW CONSTRAINTS` emits no UNIQUE rows at all - the AWS documentation gives it a PRIMARY
    KEYS form and a FOREIGN KEYS form only, while `pg_constraint` carries `p` and `u` directly.
    """

    def test_primary_and_unique_constraints_are_both_reported(
        self,
        redshift_scratch_db,
    ) -> None:
        redshift_scratch_db.execute(
            "CREATE TABLE public.uniq_test (id int PRIMARY KEY, code varchar(8) UNIQUE)",
        )
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            keys = adapter.introspect_unique_keys("public.uniq_test")
        finally:
            adapter.close()

        by_columns = {k.columns: k.primary for k in keys}
        assert by_columns.get(("id",)) is True, keys
        assert by_columns.get(("code",)) is False, keys


class TestDatabaseNameFilterIsEmitted:
    """`SVV_REDSHIFT_COLUMNS` also lists datashared columns, so a missing `database_name` filter
    lets a remote table contaminate the local list - asserted on the SQL, no substrate having one.
    """

    def test_columns_and_physical_layout_queries_are_scoped_to_the_current_database(
        self,
        redshift_scratch_db,
    ) -> None:
        redshift_scratch_db.execute("CREATE TABLE public.scoped_test (a int)")
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))
        recorder = _install_recorder(adapter)

        try:
            adapter.introspect_columns("public.scoped_test")
            adapter.introspect_physical_layout("public.scoped_test")
        finally:
            adapter.close()

        statements = recorder.flattened()
        columns_stmt = next(
            s for s in statements if "svv_redshift_columns" in s and "sortkey" not in s
        )
        layout_stmt = next(s for s in statements if "sortkey <> 0" in s)

        assert "database_name = current_database()" in columns_stmt
        assert "database_name = current_database()" in layout_stmt


class TestExtractDdlFallsBackForAView:
    """`SHOW TABLE` against a view must fall back on a server error as well as on a falsy row -
    AWS documents no contract for issuing it against the wrong kind of object.
    """

    def test_a_views_ddl_is_extracted_via_the_show_view_fallback(
        self,
        redshift_scratch_db,
    ) -> None:
        redshift_scratch_db.execute("CREATE TABLE public.ddl_source (id int)")
        redshift_scratch_db.execute(
            "CREATE VIEW public.ddl_view AS SELECT id FROM public.ddl_source",
        )
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            ddl = adapter.extract_ddl("public.ddl_view")
        finally:
            adapter.close()

        # Not just "some DDL text came back": a kind-unaware fabrication answers for a view too,
        # so only `CREATE VIEW` proves `SHOW VIEW` was reached.
        assert "CREATE VIEW" in ddl.upper(), f"the fallback did not truly run: {ddl!r}"


class TestDateColumnFullTemporalStatistics:
    """`span_days` on a DATE column must use `DATEDIFF` - `EXTRACT(EPOCH FROM ...)` takes no DATE
    difference, and a failure there costs every other temporal field, all REQUIRED on `temporal`.
    """

    def test_a_date_column_publishes_every_required_temporal_field(
        self,
        redshift_scratch_db,
    ) -> None:
        redshift_scratch_db.execute("CREATE TABLE public.date_col (d date)")
        values = ", ".join(f"('2024-01-{i + 1:02d}')" for i in range(28))
        redshift_scratch_db.execute(f"INSERT INTO public.date_col (d) VALUES {values}")
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))
        config = StatisticsConfig(enumeration_threshold=5)

        try:
            columns = adapter.introspect_columns("public.date_col")
            counts, base = adapter.compute_base_statistics("public.date_col", columns, config)
            stats = adapter.compute_column_statistics(
                "public.date_col",
                columns,
                config,
                counts,
                base,
                frozenset(),
            )
        finally:
            adapter.close()

        stat = stats["d"]
        assert stat.range is not None
        assert stat.range.min is not None and stat.range.max is not None
        assert stat.range.span_days == 27
        assert stat.percentiles
        assert stat.distribution is not None
        assert stat.frequencies is not None
        assert stat.values
        # A DATE column is always day-aligned, so `quantized_count` - whether a finer-grained
        # value lands on a day boundary - is not computed for it at all.
        assert stat.quantized_count is None


class TestTimelineBucketUsesAnchorDomainRendering:
    """A timeline bucket renders through `_render_calendar_bound`, as `range`/`percentiles` do -
    SPEC 2.2.16 requires the anchor's own domain rendering, not a second, bare one.
    """

    def test_a_timestamptz_anchors_buckets_carry_the_full_iso_rendering(
        self,
        redshift_scratch_db,
    ) -> None:
        redshift_scratch_db.execute("CREATE TABLE public.tz_timeline (logged_at timestamptz)")
        values = ", ".join(f"('2024-01-{i + 1:02d}T00:00:00Z')" for i in range(10))
        redshift_scratch_db.execute(
            f"INSERT INTO public.tz_timeline (logged_at) VALUES {values}",
        )
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            columns = adapter.introspect_columns("public.tz_timeline")
            counts, _base = adapter.compute_base_statistics(
                "public.tz_timeline",
                columns,
                StatisticsConfig(),
            )
            buckets = adapter.probe_timeline(
                "public.tz_timeline",
                columns,
                counts,
                "logged_at",
                "day",
            )
        finally:
            adapter.close()

        assert buckets, "no buckets were produced"
        for bucket_start, _count in buckets:
            assert bucket_start.endswith("Z"), (
                f"bucket text lost the anchor's own TZ-aware rendering: {bucket_start!r}"
            )
            assert "T" in bucket_start, (
                f"bucket text lost the anchor's own full ISO shape: {bucket_start!r}"
            )


class TestMaterializedViewType:
    """`SVV_REDSHIFT_TABLES.table_type` documents only 'views' and 'tables', so `STV_MV_INFO` is
    the purpose-built source for the one distinction that view lacks.
    """

    def test_a_materialized_view_is_typed_matview_not_view(self, redshift_scratch_db) -> None:
        redshift_scratch_db.execute("CREATE TABLE public.mv_base (id int)")
        redshift_scratch_db.execute(
            "CREATE MATERIALIZED VIEW public.mv_test AS SELECT id FROM public.mv_base",
        )
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))

        try:
            tables = {t.fqn: t.type for t in adapter.list_tables(include=["*"], exclude=[])}
        finally:
            adapter.close()

        assert tables["public.mv_test"] == "matview"
        assert tables["public.mv_base"] == "table"


class TestPhysicalNameUsedInStatistics:
    """Every statistics statement quotes `physical_name`, not the lowercased map key - under
    `enable_case_sensitive_identifier` a mixed-case column does not exist under the folded name.
    """

    def test_a_mixed_case_column_is_addressed_by_its_physical_name(
        self,
        redshift_scratch_db,
    ) -> None:
        redshift_scratch_db.execute('CREATE TABLE public.mixed_case ("Amount" int)')
        redshift_scratch_db.execute(
            'INSERT INTO public.mixed_case ("Amount") VALUES (1), (2), (3), (4), (5)',
        )
        adapter = _redshift_adapter(RedshiftDialectShim(redshift_scratch_db))
        config = StatisticsConfig(enumeration_threshold=1)

        try:
            columns = adapter.introspect_columns("public.mixed_case")
            assert columns[0].physical_name == "Amount"
            counts, base = adapter.compute_base_statistics(
                "public.mixed_case",
                columns,
                config,
            )
            stats = adapter.compute_column_statistics(
                "public.mixed_case",
                columns,
                config,
                counts,
                base,
                frozenset(),
            )
        finally:
            adapter.close()

        assert counts.row_count == 5
        assert base["amount"].cardinality == 5
        assert stats["amount"].cardinality == 5


class TestNullableThirdState:
    """`is_nullable` carries a documented third state, blank, meaning "no information" - never
    fabricate a positive NOT NULL claim the catalog did not make.
    """

    def test_blank_and_no_reads_as_expected(self) -> None:
        from dbprint.adapters.redshift.introspect import _nullable

        assert _nullable("YES") is True
        assert _nullable("NO") is False
        assert _nullable(" ") is True, "no information must not fabricate NOT NULL"
        assert _nullable("") is True
