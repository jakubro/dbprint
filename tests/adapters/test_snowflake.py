"""Snowflake-specific behaviors the contract suite skips; tests run against duckdb in memory."""

from __future__ import annotations

import importlib
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import duckdb
import pytest
import yaml

from dbprint.adapters import ColumnMeta, ColumnStats, SnowflakeAdapter, TableScope
from dbprint.adapters import base as adapters_base
from dbprint.adapters.base import PhaseB
from dbprint.adapters.errors import QueryFailed
from dbprint.adapters.identifiers import IdentifierRejected, Identity, UnknownTable
from dbprint.adapters.snowflake import DIALECT
from dbprint.adapters.snowflake import connection as connection_module
from dbprint.adapters.snowflake import introspect as snowflake_introspect
from dbprint.adapters.snowflake.connection import (
    ConnectionParams,
    SnowflakeConnectionError,
    _default_cursor_factory,
    _load_private_key,
)
from dbprint.config.project import RuleConfig
from dbprint.engine import Engine
from tests._prints import connection_config
from tests.adapters._credentials import SNOWFLAKE_CREDS
from tests.adapters.conftest import SnowflakeDialectShim, StubCursor


# Credential set without auth material - callers add password or private_key_file.
_BASE: dict[str, str] = {k: v for k, v in SNOWFLAKE_CREDS.items() if k != "password"}


def _write_rsa_key(path: Path, *, passphrase: str | None = None) -> Path:
    """Write a throwaway PEM PKCS8 RSA private key (optionally encrypted)."""

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    encryption = (
        serialization.BestAvailableEncryption(passphrase.encode())
        if passphrase
        else serialization.NoEncryption()
    )
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            encryption,
        ),
    )

    return path


class _FakeConnection:
    def cursor(self) -> object:
        return object()


class _FakeConnector:
    """Stand-in for snowflake.connector that records connect() kwargs."""

    def __init__(self, captured: dict[str, object]) -> None:
        self._captured = captured

    def connect(self, **kwargs: object) -> _FakeConnection:
        self._captured.update(kwargs)

        return _FakeConnection()


@pytest.fixture
def fresh_duckdb() -> duckdb.DuckDBPyConnection:
    """Per-test in-memory duckdb instance; caller seeds schema as needed."""

    con = duckdb.connect(":memory:")
    # duckdb repeats a sample only single-threaded; more threads vary the result despite the seed.
    con.execute("SET threads = 1")

    return con


def _build_adapter(duckdb_conn: duckdb.DuckDBPyConnection) -> SnowflakeAdapter:
    shim = SnowflakeDialectShim(duckdb_conn)
    a = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: shim)
    a.connect()

    return a


class TestConnectionParams:
    def test_missing_credential_key_raises(self) -> None:
        incomplete = {k: v for k, v in SNOWFLAKE_CREDS.items() if k != "warehouse"}

        with pytest.raises(SnowflakeConnectionError, match="warehouse"):
            ConnectionParams.from_credentials(incomplete)

    def test_optional_schema_supported(self) -> None:
        params = ConnectionParams.from_credentials({**SNOWFLAKE_CREDS, "schema": "SEEDBANK"})
        assert params.schema == "SEEDBANK"

    def test_default_factory_without_extra_raises(self) -> None:
        """Import fails without the [snowflake] extra (not installed in dev; tests use duckdb)."""

        adapter = SnowflakeAdapter(SNOWFLAKE_CREDS)

        with pytest.raises(SnowflakeConnectionError, match=r"dbprint\[snowflake\]"):
            adapter.connect()


class TestKeyPairAuth:
    def test_key_only_auth_ok(self) -> None:
        params = ConnectionParams.from_credentials({**_BASE, "private_key_file": "/k.pem"})

        assert params.private_key_file == "/k.pem"
        assert params.password is None

    def test_both_auth_methods_raise(self) -> None:
        with pytest.raises(SnowflakeConnectionError, match="exactly one"):
            ConnectionParams.from_credentials(
                {**_BASE, "password": "pw", "private_key_file": "/k.pem"},
            )

    def test_neither_auth_method_raises(self) -> None:
        with pytest.raises(SnowflakeConnectionError, match="exactly one"):
            ConnectionParams.from_credentials(dict(_BASE))

    def test_password_connect_kwargs_carry_password_not_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            importlib,
            "import_module",
            lambda _name: _FakeConnector(captured),
        )
        params = ConnectionParams.from_credentials({**_BASE, "password": "pw"})

        _default_cursor_factory(params)

        assert captured["password"] == "pw"
        assert "private_key" not in captured

    def test_keypair_connect_kwargs_carry_private_key_not_password(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        key_path = _write_rsa_key(tmp_path / "rsa.pem")
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            importlib,
            "import_module",
            lambda _name: _FakeConnector(captured),
        )
        params = ConnectionParams.from_credentials(
            {**_BASE, "private_key_file": str(key_path), "schema": "SEEDBANK"},
        )

        _default_cursor_factory(params)

        assert isinstance(captured["private_key"], bytes)
        assert "password" not in captured
        assert captured["schema"] == "SEEDBANK"  # optional schema round-trips

    def test_encrypted_key_decrypts_with_passphrase(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        key_path = _write_rsa_key(tmp_path / "rsa.pem", passphrase="secret")
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            importlib,
            "import_module",
            lambda _name: _FakeConnector(captured),
        )
        params = ConnectionParams.from_credentials(
            {**_BASE, "private_key_file": str(key_path), "private_key_file_pwd": "secret"},
        )

        _default_cursor_factory(params)

        assert isinstance(captured["private_key"], bytes)

    def test_bad_key_file_raises_actionable_error(self, tmp_path: Path) -> None:
        bad = tmp_path / "garbage.pem"
        bad.write_text("not a pem key\n")

        with pytest.raises(SnowflakeConnectionError, match=r"could not load.*garbage\.pem"):
            _load_private_key(str(bad), None)

    def test_missing_key_file_raises_actionable_error(self, tmp_path: Path) -> None:
        with pytest.raises(SnowflakeConnectionError, match="could not load"):
            _load_private_key(str(tmp_path / "absent.pem"), None)


class TestStatementTrace:
    """exec_query's own DEBUG record - statement, params, elapsed, rows."""

    def test_success_logs_statement_params_and_rows(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="dbprint.adapters.snowflake.connection"):
            connection_module.exec_query(StubCursor(rowcount=3), "SELECT ?", ("x",))

        assert "SELECT ?" in caplog.text
        assert "rows=3" in caplog.text

    def test_failure_logs_before_the_exception_propagates(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger="dbprint.adapters.snowflake.connection"),
            pytest.raises(QueryFailed),
        ):
            connection_module.exec_query(StubCursor(error=RuntimeError("boom")), "SELECT 1")

        assert "statement failed" in caplog.text


class TestExecuteQueryTrace:
    """The SQL-assertion seam (`check --online`'s `sql:` block) is traced like any statement."""

    def test_the_operators_statement_is_traced(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        adapter = _build_adapter(fresh_duckdb)

        with caplog.at_level(logging.DEBUG, logger="dbprint.adapters.snowflake.connection"):
            adapter.execute_query("SELECT 1 AS n")

        # duckdb (the Snowflake test substrate) reports no meaningful `.rowcount` for a
        # SELECT - the driver-dependent omission this seam already handles, not a defect.
        assert "SELECT 1 AS n" in caplog.text
        assert "elapsed_ms=" in caplog.text


class TestFqnFormat:
    def test_three_part_fqn(self, fresh_duckdb: duckdb.DuckDBPyConnection) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute("CREATE TABLE seedbank.specimen_loan (id INTEGER PRIMARY KEY)")

        adapter = _build_adapter(fresh_duckdb)
        tables = adapter.list_tables(include=["*"], exclude=[])

        assert len(tables) == 1
        # FQN is database.schema.table; all lowercase
        assert tables[0].fqn == "memory.seedbank.specimen_loan"
        assert tables[0].namespace_path == ("memory", "seedbank", "specimen_loan")


class TestTimeOnlyColumns:
    def test_time_column_avoids_date_arithmetic(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """A time of day has no date, so DATEDIFF against CURRENT_TIMESTAMP is undefined."""

        from dbprint.config import StatisticsConfig

        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute("CREATE TABLE seedbank.field_watch (id INTEGER, collected_at TIME)")
        fresh_duckdb.execute(
            "INSERT INTO seedbank.field_watch SELECT i, TIME '08:00:00' + INTERVAL (i % 60) MINUTE "
            "FROM range(80) t(i)",
        )
        recorder = _RecordingShim(fresh_duckdb)
        adapter = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: recorder)
        adapter.connect()
        adapter.list_tables(include=["*"], exclude=[])
        cols = adapter.introspect_columns("memory.seedbank.field_watch")

        _, stats = adapter.compute_statistics(
            "memory.seedbank.field_watch",
            cols,
            StatisticsConfig(),
            frozenset(),
        )

        # Scoped to `collected_at`: `id` is a fully-unique integer, so it classifies numeric
        # (SPEC 4.2) and emits its own WITHIN GROUP percentile query.
        collected_at_statements = [s for s in recorder.statements if "collected_at" in s]

        assert not any("DATEDIFF(" in s for s in collected_at_statements)
        # TIME shares the timestamp percentile path; Snowflake rejects WITHIN GROUP over either.
        assert not any("WITHIN GROUP" in s for s in collected_at_statements)
        # Still conformant: temporal columns must carry range.
        assert stats["collected_at"].range is not None
        assert stats["collected_at"].range.span_days == 0
        assert stats["collected_at"].percentiles


class TestSystemSchemaNames:
    def test_schema_named_main_is_profiled_not_skipped(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """`main` is another engine's default schema, not a Snowflake system one."""

        fresh_duckdb.execute("CREATE TABLE main.specimen_loan (id INTEGER PRIMARY KEY)")
        adapter = _build_adapter(fresh_duckdb)

        tables = adapter.list_tables(include=["*"], exclude=[])

        assert "memory.main.specimen_loan" in {t.fqn for t in tables}


class TestViewHandling:
    def test_view_listed_as_view_type(self, fresh_duckdb: duckdb.DuckDBPyConnection) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute(
            "CREATE TABLE seedbank.specimen_loan (id INTEGER PRIMARY KEY, viability_pct INTEGER)",
        )
        fresh_duckdb.execute(
            "CREATE VIEW seedbank.specimen_loan_summary AS SELECT * FROM seedbank.specimen_loan "
            "WHERE viability_pct > 100",
        )

        adapter = _build_adapter(fresh_duckdb)
        tables = adapter.list_tables(include=["*"], exclude=[])
        types = {t.fqn: t.type for t in tables}

        assert types["memory.seedbank.specimen_loan"] == "table"
        assert types["memory.seedbank.specimen_loan_summary"] == "view"

    def test_view_ddl_extractable(self, fresh_duckdb: duckdb.DuckDBPyConnection) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute("CREATE TABLE seedbank.specimen_loan (id INTEGER PRIMARY KEY)")
        fresh_duckdb.execute("CREATE VIEW seedbank.v AS SELECT id FROM seedbank.specimen_loan")

        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])
        ddl = adapter.extract_ddl("memory.seedbank.v")
        assert "v" in ddl.lower()
        assert ddl.endswith("\n")


class _RecordingShim(SnowflakeDialectShim):
    """Dialect shim that also records every statement the adapter emits."""

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        table_types: dict[str, str] | None = None,
    ) -> None:
        super().__init__(con, table_types=table_types)
        self.statements: list[str] = []

    def execute(self, sql: str, params: object = None) -> _RecordingShim:
        self.statements.append(" ".join(sql.split()))
        super().execute(sql, params)

        return self


def _seed_wide(con: duckdb.DuckDBPyConnection) -> None:
    """Schema reaching the temporal + sampling paths the 3-row fixture cannot.

    `seen_at` repeats so it clears the enumeration threshold into the temporal block, and
    80 rows clears the large-table sampling cutoff.
    """

    con.execute("CREATE SCHEMA seedbank")
    con.execute(
        "CREATE TABLE seedbank.curation_event (id INTEGER, seen_at TIMESTAMP, label VARCHAR)",
    )
    con.execute(
        "INSERT INTO seedbank.curation_event "
        "SELECT i, TIMESTAMP '2024-01-01 00:00:00' + INTERVAL (i % 60) DAY, 'l' || i "
        "FROM range(80) t(i)",
    )


class TestEmittedDialect:
    """Every statement the adapter emits must be Snowflake dialect, not duckdb."""

    def _record(self, con: duckdb.DuckDBPyConnection) -> _RecordingShim:
        from dbprint.config import StatisticsConfig

        recorder = _RecordingShim(con)
        adapter = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: recorder)
        adapter.connect()

        for table in adapter.list_tables(include=["*"], exclude=[]):
            adapter.extract_ddl(table.fqn)
            cols = adapter.introspect_columns(table.fqn)
            adapter.introspect_indexes(table.fqn)
            adapter.extract_comments(table.fqn)
            adapter.compute_statistics(table.fqn, cols, StatisticsConfig(), frozenset())

            for col in cols:
                adapter.sample_values(table.fqn, col.name, n=5)

        return recorder

    def test_no_duckdb_vendor_functions_emitted(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        _seed_wide(fresh_duckdb)
        offenders = [s for s in self._record(fresh_duckdb).statements if "duckdb_" in s.lower()]

        assert offenders == []

    def test_ddl_uses_get_ddl(self, fresh_duckdb: duckdb.DuckDBPyConnection) -> None:
        _seed_wide(fresh_duckdb)
        statements = self._record(fresh_duckdb).statements

        assert any("GET_DDL" in s for s in statements)

    def test_null_counts_use_count_if(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        _seed_wide(fresh_duckdb)
        statements = self._record(fresh_duckdb).statements

        assert any("COUNT_IF(" in s for s in statements)

    def test_comments_and_indexes_read_information_schema(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        _seed_wide(fresh_duckdb)
        statements = self._record(fresh_duckdb).statements

        assert any(
            " ".join(s.split()).startswith(
                'SELECT tbl.comment FROM "memory".information_schema.tables tbl',
            )
            for s in statements
        )
        assert any('"memory".information_schema.indexes' in s for s in statements)

    def test_temporal_stats_count_days_with_datediff(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        _seed_wide(fresh_duckdb)
        statements = self._record(fresh_duckdb).statements
        temporal = [s for s in statements if "span_days" in s]

        assert temporal, "the temporal statistics path must be exercised"
        assert all("DATEDIFF(" in s for s in temporal)

    def test_no_percentile_aggregate_orders_by_a_temporal_column(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """Snowflake resolves a percentile's ORDER BY against a fixed-point numeric, so a
        timestamp does not compile there - and duckdb accepts it, so only this catches it.
        """

        _seed_wide(fresh_duckdb)
        statements = self._record(fresh_duckdb).statements

        assert any("span_days" in s for s in statements), (
            "the temporal path emitted nothing, so the absence below proves nothing"
        )

        offenders = [
            s
            for s in statements
            if re.search(
                r'PERCENTILE_\w+\s*\([^)]*\)\s*WITHIN\s+GROUP\s*\(\s*ORDER\s+BY\s+"seen_at"',
                s,
                re.IGNORECASE,
            )
        ]

        assert not offenders, (
            "a percentile aggregate ordered by a temporal column; Snowflake rejects this with "
            "'incompatible types: [TIMESTAMP_NTZ(9)] and [NUMBER(9,0)]': " + "; ".join(offenders)
        )

    def test_sampling_uses_snowflake_grammar_with_literal_count(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        _seed_wide(fresh_duckdb)
        statements = self._record(fresh_duckdb).statements
        sampled = [s for s in statements if "SAMPLE" in s]

        assert sampled, "the large-table sampling path must be exercised"
        assert all("SAMPLE ROW (" in s for s in sampled)

    def test_no_placeholder_left_in_row_limits(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        _seed_wide(fresh_duckdb)
        statements = self._record(fresh_duckdb).statements

        assert not any("LIMIT ?" in s for s in statements)


def _seed_temporal(con: duckdb.DuckDBPyConnection, *, rows: int, distinct: int) -> None:
    """Seed `seedbank.curation_event` with `rows` rows spread over `distinct` timestamps."""

    con.execute("CREATE SCHEMA seedbank")
    con.execute("CREATE TABLE seedbank.curation_event (id INTEGER, seen_at TIMESTAMP)")
    con.execute(
        "INSERT INTO seedbank.curation_event "
        f"SELECT i, TIMESTAMP '2026-01-01 12:00:00' + INTERVAL (i % {distinct}) DAY "
        f"FROM range({rows}) t(i)",
    )


def _temporal_stats(con: duckdb.DuckDBPyConnection) -> PhaseB:
    """Run the adapter over the seeded table and return its column stats."""

    from dbprint.config import StatisticsConfig

    adapter = _build_adapter(con)
    adapter.list_tables(include=["*"], exclude=[])
    columns = adapter.introspect_columns("memory.seedbank.curation_event")

    _, stats = adapter.compute_statistics(
        "memory.seedbank.curation_event",
        columns,
        StatisticsConfig(),
        frozenset(),
    )

    return stats


class TestTemporalPercentiles:
    """Percentile values match PERCENTILE_DISC without invoking it.

    The aggregate is unusable on Snowflake for a temporal column, so the adapter takes the
    value at the rank it is defined by; duckdb can still evaluate it as the reference.
    """

    # The last case discriminates: with integral `p * n` or wide value groups, rounding and
    # flooring land in the same group; narrow groups plus fractional cut-points separate them.
    @pytest.mark.parametrize(
        ("rows", "distinct"),
        [(200, 60), (199, 61), (150, 51), (151, 101)],
        ids=["even-rows", "odd-rows", "just-above-threshold", "narrow-value-groups"],
    )
    def test_values_match_the_ordered_set_aggregate(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
        rows: int,
        distinct: int,
    ) -> None:
        from dbprint.config import StatisticsConfig

        _seed_temporal(fresh_duckdb, rows=rows, distinct=distinct)
        percentiles = _temporal_stats(fresh_duckdb)["seen_at"].percentiles

        assert percentiles, "the temporal branch must have produced percentiles"

        aggregate = ", ".join(
            f"PERCENTILE_DISC({p / 100.0}) WITHIN GROUP (ORDER BY seen_at)"
            for p in StatisticsConfig().percentiles
        )
        expected = fresh_duckdb.execute(
            f"SELECT {aggregate} FROM seedbank.curation_event WHERE seen_at IS NOT NULL",
        ).fetchone()

        assert expected is not None, "the reference aggregate returned no row"
        assert [datetime.fromisoformat(v) for v in percentiles.values()] == list(expected)

    def test_sub_threshold_column_enumerates_values_instead(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """Sub-threshold temporal columns are categorical: value map, no percentiles."""

        _seed_temporal(fresh_duckdb, rows=40, distinct=5)
        stats = _temporal_stats(fresh_duckdb)["seen_at"]

        assert stats.values
        assert stats.percentiles is None

    def test_single_row_table_extracts(self, fresh_duckdb: duckdb.DuckDBPyConnection) -> None:
        """A single row makes the column categorical; the ranked temporal query is never reached."""

        _seed_temporal(fresh_duckdb, rows=1, distinct=1)
        stats = _temporal_stats(fresh_duckdb)["seen_at"]

        assert stats.cardinality == 1
        assert stats.percentiles is None


class TestOutOfRangeTemporal:
    """A year outside 0001-9999 does not crash conversion of the fetched row."""

    @staticmethod
    def _seed(con: duckdb.DuckDBPyConnection, values: list[str]) -> None:
        _seed_temporal(con, rows=240, distinct=60)

        for offset, value in enumerate(values):
            con.execute(
                f"INSERT INTO seedbank.curation_event VALUES ({900 + offset}, TIMESTAMP '{value}')",
            )

    def test_year_beyond_9999_profiles_and_is_marked(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        self._seed(fresh_duckdb, ["52030-01-01 00:00:00"])
        stats = _temporal_stats(fresh_duckdb)["seen_at"]

        assert stats.range is not None
        assert stats.range.max == "52030-01-01T00:00:00"
        assert stats.unrepresentable == ("max",)

    def test_ordinary_values_render_byte_identical(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """Ordinary values render as `isoformat()` would, unaffected by the handling above."""

        self._seed(fresh_duckdb, [])
        stats = _temporal_stats(fresh_duckdb)["seen_at"]

        assert stats.range is not None
        expected_min = datetime(2026, 1, 1, 12, 0, 0)  # noqa: DTZ001 - naive-column expectation
        expected_max = expected_min + timedelta(days=59)
        assert stats.range.min == expected_min.isoformat()
        assert stats.range.max == expected_max.isoformat()
        assert stats.unrepresentable is None


class TestImportedKeys:
    """FK columns come from SHOW IMPORTED KEYS; Snowflake has no KEY_COLUMN_USAGE."""

    def _fk_schema(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute("CREATE SCHEMA seedbank")
        con.execute("CREATE TABLE seedbank.parent (a INTEGER, b INTEGER, PRIMARY KEY (a, b))")
        con.execute(
            "CREATE TABLE seedbank.child ("
            "  id INTEGER PRIMARY KEY, a INTEGER, b INTEGER,"
            "  FOREIGN KEY (a, b) REFERENCES seedbank.parent(a, b)"
            ")",
        )

    def test_key_column_usage_is_never_queried(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        self._fk_schema(fresh_duckdb)
        recorder = _RecordingShim(fresh_duckdb)
        adapter = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: recorder)
        adapter.connect()

        for table in adapter.list_tables(include=["*"], exclude=[]):
            adapter.introspect_relationships(table.fqn)

        assert not any("key_column_usage" in s.lower() for s in recorder.statements)
        assert any(s.startswith("SHOW IMPORTED KEYS IN TABLE") for s in recorder.statements)

    def test_composite_order_and_target_resolve(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        self._fk_schema(fresh_duckdb)
        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])

        fks = adapter.introspect_relationships("memory.seedbank.child")

        assert len(fks) == 1
        assert fks[0].column == ("a", "b")
        assert fks[0].target_column == ("a", "b")
        assert fks[0].target_table == "memory.seedbank.parent"
        assert fks[0].constraint_name

    def test_self_referential_fk(self, fresh_duckdb: duckdb.DuckDBPyConnection) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute(
            "CREATE TABLE seedbank.node (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES seedbank.node(id))",
        )
        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])

        fks = adapter.introspect_relationships("memory.seedbank.node")

        assert len(fks) == 1
        assert fks[0].column == ("parent_id",)
        assert fks[0].target_table == "memory.seedbank.node"

    def test_table_without_fks_yields_empty_list(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute("CREATE TABLE seedbank.solo (id INTEGER PRIMARY KEY)")
        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])

        assert adapter.introspect_relationships("memory.seedbank.solo") == []


class TestPhysicalIdentifierCase:
    """Snowflake reports identifiers uppercase; statements must address that form.

    duckdb keeps a quoted identifier's case and compares catalog strings case-sensitively, so
    an uppercase-quoted schema stands in for Snowflake's storage.
    """

    def _uppercase_catalog(self, con: duckdb.DuckDBPyConnection) -> None:
        con.execute('CREATE SCHEMA "SEEDBANK"')
        con.execute('CREATE TABLE "SEEDBANK"."CURATION_EVENT" ("ID" INTEGER, "LABEL" VARCHAR)')
        con.execute("INSERT INTO \"SEEDBANK\".\"CURATION_EVENT\" VALUES (1, 'a'), (2, 'b')")

    def test_paths_are_lowercase_while_columns_still_resolve(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        self._uppercase_catalog(fresh_duckdb)
        adapter = _build_adapter(fresh_duckdb)

        tables = adapter.list_tables(include=["*"], exclude=[])
        assert [t.fqn for t in tables] == ["memory.seedbank.curation_event"]

        cols = adapter.introspect_columns("memory.seedbank.curation_event")
        assert [c.name for c in cols] == ["id", "label"]

    def test_physical_name_carries_the_catalog_spelling(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """SPEC 2.2.4: detection (SPEC 4.4.3) reads this, not the lowercased map key."""

        self._uppercase_catalog(fresh_duckdb)
        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])

        cols = {c.name: c for c in adapter.introspect_columns("memory.seedbank.curation_event")}

        assert cols["id"].physical_name == "ID"
        assert cols["label"].physical_name == "LABEL"

    def test_statistics_address_the_physical_table(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        from dbprint.config import StatisticsConfig

        self._uppercase_catalog(fresh_duckdb)
        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])
        cols = adapter.introspect_columns("memory.seedbank.curation_event")

        counts, stats = adapter.compute_statistics(
            "memory.seedbank.curation_event",
            cols,
            StatisticsConfig(),
            frozenset(),
        )

        assert counts.row_count == 2
        assert set(stats) == {"id", "label"}

    def test_the_path_decision_reads_the_catalog_not_a_count(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """Deciding affordability must not cost a scan: a row count is a full scan per column."""

        _seed_wide(fresh_duckdb)
        recorder = _RecordingShim(fresh_duckdb)
        adapter = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: recorder)
        adapter.connect()
        adapter.list_tables(include=["*"], exclude=[])
        adapter.introspect_columns("memory.seedbank.curation_event")
        recorder.statements.clear()

        adapter.sample_values("memory.seedbank.curation_event", "label", n=5)

        assert recorder.statements, "the sampler emitted nothing; the check would be vacuous"

        counted = [s for s in recorder.statements if re.search(r"COUNT\((\*|1)\)", s.upper())]

        assert not counted, f"the sampler counted rows to choose a path: {counted}"
        assert any("INFORMATION_SCHEMA.TABLES" in s.upper() for s in recorder.statements), (
            "the sampler read neither a count nor the catalog, so it decided on nothing"
        )

    def test_a_scoped_draw_reads_the_scoped_source(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """A predicate reaches the sampler's own statement, not just the statistics."""

        _seed_wide(fresh_duckdb)
        recorder = _RecordingShim(fresh_duckdb)
        adapter = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: recorder)
        adapter.connect()
        adapter.list_tables(include=["*"], exclude=[])
        adapter.introspect_columns("memory.seedbank.curation_event")
        recorder.statements.clear()

        values = adapter.sample_values(
            "memory.seedbank.curation_event",
            "label",
            n=5,
            scope=TableScope(filter='"id" < 10'),
        )

        assert all(int(v.removeprefix("l")) < 10 for v in values)

    def test_sampling_addresses_the_physical_column(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        self._uppercase_catalog(fresh_duckdb)
        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])
        adapter.introspect_columns("memory.seedbank.curation_event")

        assert sorted(adapter.sample_values("memory.seedbank.curation_event", "label", n=5)) == [
            "a",
            "b",
        ]

    def test_unlisted_table_raises_instead_of_guessing(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        self._uppercase_catalog(fresh_duckdb)
        adapter = _build_adapter(fresh_duckdb)

        with pytest.raises(UnknownTable, match="list_tables"):
            adapter.introspect_columns("memory.seedbank.curation_event")

    def test_case_collision_is_rejected(self) -> None:
        rows: list[tuple[object, ...]] = [
            ("MEMORY", "SEEDBANK", "Curator", "BASE TABLE"),
            ("MEMORY", "SEEDBANK", "CURATOR", "BASE TABLE"),
        ]
        adapter = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: StubCursor(rows))
        adapter.connect()

        with pytest.raises(IdentifierRejected, match="case-collides-with"):
            adapter.list_tables(include=["*"], exclude=[])

    @pytest.mark.parametrize(
        ("defined", "read"),
        [("v", '"v"'), ('"v"', "v"), ('"V"', '"v"')],
    )
    def test_a_derived_column_read_in_another_case_is_refused(
        self,
        defined: str,
        read: str,
    ) -> None:
        shim = SnowflakeDialectShim(duckdb.connect(":memory:"))

        with pytest.raises(AssertionError, match=f"invalid identifier src.{read}"):
            shim.execute(f"SELECT src.{read} FROM (SELECT 1 AS {defined}) src")

    @pytest.mark.parametrize(
        ("defined", "read"),
        [("v", "v"), ("v", '"V"'), ('"v"', '"v"')],
    )
    def test_a_derived_column_read_as_snowflake_resolves_it_runs(
        self,
        defined: str,
        read: str,
    ) -> None:
        shim = SnowflakeDialectShim(duckdb.connect(":memory:"))

        assert shim.execute(f"SELECT src.{read} FROM (SELECT 1 AS {defined}) src").fetchall() == [
            (1,),
        ]


class TestCollation:
    """SPEC 2.2.2/2.2.4: `default_collation` is documented, not queried - Snowflake carries
    no database- or session-level default.
    """

    def test_default_collation_is_the_documented_constant(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        adapter = _build_adapter(fresh_duckdb)

        assert adapter.default_collation() == "utf8_binary"


class TestPhysicalLayout:
    """The declared clustering key, decoded from `SHOW TABLES` into `PhysicalLayout`."""

    def _adapter(
        self,
        con: duckdb.DuckDBPyConnection,
        cluster_by: dict[str, str],
    ) -> SnowflakeAdapter:
        shim = SnowflakeDialectShim(con, cluster_by=cluster_by)
        a = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: shim)
        a.connect()

        return a

    def test_a_single_column_key_is_recovered(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute("CREATE TABLE seedbank.curation_event (id INTEGER)")
        adapter = self._adapter(fresh_duckdb, {"curation_event": "LINEAR(ID)"})
        adapter.list_tables(include=["*"], exclude=[])

        layout = adapter.introspect_physical_layout("memory.seedbank.curation_event")

        assert layout is not None
        assert layout.mechanism == "cluster"
        assert [k.expression for k in layout.keys] == ["ID"]
        assert [k.column for k in layout.keys] == ["id"]

    def test_an_expression_key_still_yields_its_base_column(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute(
            "CREATE TABLE seedbank.curation_event (id INTEGER, logged_at TIMESTAMP)",
        )
        adapter = self._adapter(fresh_duckdb, {"curation_event": "LINEAR(LOGGED_AT::DATE)"})
        adapter.list_tables(include=["*"], exclude=[])

        layout = adapter.introspect_physical_layout("memory.seedbank.curation_event")

        assert layout is not None
        key = layout.keys[0]
        assert key.expression == "LOGGED_AT::DATE"
        assert key.column == "logged_at"

    def test_a_multi_column_key_preserves_declaration_order(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute(
            "CREATE TABLE seedbank.curation_event (vault_id INTEGER, reading_id INTEGER)",
        )
        adapter = self._adapter(fresh_duckdb, {"curation_event": "LINEAR(VAULT_ID, READING_ID)"})
        adapter.list_tables(include=["*"], exclude=[])

        layout = adapter.introspect_physical_layout("memory.seedbank.curation_event")

        assert layout is not None
        assert [k.column for k in layout.keys] == ["vault_id", "reading_id"]

    def test_an_unclustered_table_reports_absence(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute("CREATE TABLE seedbank.curation_event (id INTEGER)")
        adapter = self._adapter(fresh_duckdb, {})
        adapter.list_tables(include=["*"], exclude=[])

        assert adapter.introspect_physical_layout("memory.seedbank.curation_event") is None


class TestViewDependencies:
    """`introspect_view_dependencies` via `ACCOUNT_USAGE.OBJECT_DEPENDENCIES` - the shim proves
    row-shape handling only, never real Snowflake dependency resolution.
    """

    def _adapter(self, con: duckdb.DuckDBPyConnection) -> SnowflakeAdapter:
        shim = SnowflakeDialectShim(con)
        a = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: shim)
        a.connect()
        # Dependencies are read in the namespaces the listing selected from.
        a.list_tables(include=["*"], exclude=[])

        return a

    def test_a_view_reading_two_tables_lists_both(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute("CREATE TABLE seedbank.taxon (id INTEGER)")
        fresh_duckdb.execute("CREATE TABLE seedbank.herbarium (id INTEGER)")
        fresh_duckdb.execute(
            "CREATE VIEW seedbank.both_v AS "
            "SELECT taxon.id AS a, herbarium.id AS b FROM seedbank.taxon, seedbank.herbarium",
        )
        adapter = self._adapter(fresh_duckdb)

        deps = adapter.introspect_view_dependencies()

        assert deps is not None
        assert set(deps["memory.seedbank.both_v"]) == {
            "memory.seedbank.taxon",
            "memory.seedbank.herbarium",
        }

    def test_a_view_reading_nothing_answers_with_an_empty_tuple(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute("CREATE VIEW seedbank.literal_v AS SELECT 1 AS x")
        adapter = self._adapter(fresh_duckdb)

        deps = adapter.introspect_view_dependencies()

        assert deps is not None
        assert deps["memory.seedbank.literal_v"] == ()


class _RecordingCursor:
    """Wraps a real Snowflake-shim cursor; records every statement text verbatim."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.statements: list[str] = []

    def execute(self, sql: object, params: object = None) -> _RecordingCursor:
        self.statements.append(str(sql))
        self._real.execute(sql, params)

        return self

    def fetchall(self) -> Any:
        return self._real.fetchall()

    def fetchone(self) -> Any:
        return self._real.fetchone()

    def close(self) -> None:
        self._real.close()


class TestHashOrderedDraw:
    """SPEC 4.1.2: the distinct draw is ordered by a hash of the value, not storage order."""

    def test_a_value_inserted_last_is_still_reachable(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """5,000 rows stays on the small path, where n=1000 clears `n * SMALL_TABLE_FACTOR`."""

        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute("CREATE TABLE seedbank.late_shape (v VARCHAR)")
        fresh_duckdb.execute(
            "INSERT INTO seedbank.late_shape SELECT 'row-' || i FROM range(4000) t(i)",
        )
        fresh_duckdb.execute(
            "INSERT INTO seedbank.late_shape "
            "SELECT '11111111-1111-4111-8111-' || lpad(i::VARCHAR, 12, '0') "
            "FROM range(1000) t(i)",
        )

        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])
        adapter.introspect_columns("memory.seedbank.late_shape")

        values = adapter.sample_values("memory.seedbank.late_shape", "v", n=1000)
        late = [v for v in values if str(v).startswith("11111111-1111-4111-8111-")]

        assert late, "the draw never reached a value inserted after the first n rows"

    def test_two_draws_over_unchanged_data_agree(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """The hash order is a function of the table's own seed, not session state.

        n=1000 keeps this on the small path: Snowflake's fixed-size `SAMPLE ROW` has no seed
        at all (ARCHITECTURE.md's draw table), so only the small path repeats call to call.
        """

        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute("CREATE TABLE seedbank.stable_draw (v VARCHAR)")
        fresh_duckdb.execute(
            "INSERT INTO seedbank.stable_draw SELECT 'val-' || i FROM range(5000) t(i)",
        )

        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])
        adapter.introspect_columns("memory.seedbank.stable_draw")

        first = adapter.sample_values("memory.seedbank.stable_draw", "v", n=1000)
        second = adapter.sample_values("memory.seedbank.stable_draw", "v", n=1000)

        assert first == second


def _seed_numeric(
    con: duckdb.DuckDBPyConnection,
    *,
    rows: int,
    distinct: int,
    sql_type: str,
) -> None:
    """Seed `seedbank.specimen_batch` with a wide-magnitude numeric column.

    Values sit near the top of NUMBER(20,6), where percentile interpolation overflows, and
    repeat so the column clears the enumeration threshold.
    """

    con.execute("CREATE SCHEMA seedbank")
    con.execute(f"CREATE TABLE seedbank.specimen_batch (id INTEGER, viability_pct {sql_type})")
    con.execute(
        "INSERT INTO seedbank.specimen_batch "
        f"SELECT i, (99999999999999.999999 - (i % {distinct}) * 1000000000.5)::{sql_type} "
        f"FROM range({rows}) t(i)",
    )


class TestNumericPercentileTyping:
    """A high-precision numeric column must not order the percentile by a fixed-point value.

    Snowflake types the interpolation as a multiplication against the column, so NUMBER(20,6)
    yields FIXED(23,9) and overflows at the top of the column's own range; ordering by a
    64-bit float avoids the fixed-point arithmetic, lossless under the 6-figure emission rule.
    """

    def test_percentile_orders_by_a_float_expression(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        _seed_numeric(fresh_duckdb, rows=200, distinct=60, sql_type="DECIMAL(20,6)")
        recorder = _RecordingShim(fresh_duckdb)
        adapter = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: recorder)
        adapter.connect()
        adapter.list_tables(include=["*"], exclude=[])
        columns = adapter.introspect_columns("memory.seedbank.specimen_batch")

        from dbprint.config import StatisticsConfig

        adapter.compute_statistics(
            "memory.seedbank.specimen_batch",
            columns,
            StatisticsConfig(),
            frozenset(),
        )

        percentile_statements = [s for s in recorder.statements if "PERCENTILE_CONT" in s]

        assert percentile_statements, "the numeric branch must have run"

        for statement in percentile_statements:
            assert "WITHIN GROUP (ORDER BY CAST(" in statement, statement
            assert "AS DOUBLE)" in statement, statement

    def test_percentile_values_match_the_unwidened_aggregate(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """The cast changes the arithmetic Snowflake uses, not the answer."""

        from dbprint.config import StatisticsConfig

        _seed_numeric(fresh_duckdb, rows=200, distinct=60, sql_type="DECIMAL(20,6)")
        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])
        columns = adapter.introspect_columns("memory.seedbank.specimen_batch")
        _, stats = adapter.compute_statistics(
            "memory.seedbank.specimen_batch",
            columns,
            StatisticsConfig(),
            frozenset(),
        )
        percentiles = stats["viability_pct"].percentiles

        # The unwidened PERCENTILE_CONT over the same seed, read as the nearest float.
        assert percentiles == {
            "p01": 99940999999970.5,
            "p25": 99956999999978.5,
            "p50": 99973999999987.0,
            "p75": 99987999999994.0,
            "p99": 100000000000000.0,
        }


class TestLowCardinalityTemporalSerializes:
    """A sub-threshold temporal column is categorical, so its values become artifact map keys.

    SPEC 2.2.4 restricts those keys to strings, numbers and booleans, and a raw driver object
    there aborts the whole table at dump time. The suite's other TIME coverage sits above the
    threshold, so this path needs its own seed.
    """

    @pytest.mark.parametrize(
        ("sql_type", "expression"),
        [
            ("TIME", "TIME '08:00:00' + INTERVAL (i % 6) HOUR"),
            ("DATE", "DATE '2026-01-01' + INTERVAL (i % 6) DAY"),
            ("TIMESTAMP", "TIMESTAMP '2026-01-01 08:00:00' + INTERVAL (i % 6) HOUR"),
        ],
        ids=["time", "date", "timestamp"],
    )
    def test_value_list_entries_survive_the_dump(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
        sql_type: str,
        expression: str,
    ) -> None:
        from dbprint.config import StatisticsConfig
        from dbprint.engine.yaml_dumper import dump_yaml

        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute(f"CREATE TABLE seedbank.field_round (id INTEGER, run_at {sql_type})")
        fresh_duckdb.execute(
            f"INSERT INTO seedbank.field_round SELECT i, {expression} FROM range(80) t(i)",
        )
        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])
        columns = adapter.introspect_columns("memory.seedbank.field_round")
        _, stats = adapter.compute_statistics(
            "memory.seedbank.field_round",
            columns,
            StatisticsConfig(),
            frozenset(),
        )
        values = stats["run_at"].values

        assert values, "a sub-threshold temporal column must carry a value list"
        assert all(isinstance(entry.value, str) for entry in values), values

        payload = [{"value": entry.value, "count": entry.count} for entry in values]

        assert yaml.safe_load(dump_yaml({"values": payload}))["values"]


class TestCategoricalTimestampValuesMatchRangeFrame:
    """A low-cardinality TIMESTAMPTZ's `values` render in the frame `range` uses.

    A non-UTC session is unreachable on this substrate: duckdb needs `pytz`, not a project
    dependency, to build a Python object from a TIMESTAMPTZ at all. The rendered path never
    leaves SQL text, so it needs no such dependency.
    """

    def test_value_list_carries_the_same_frame_as_range(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        from dbprint.config import StatisticsConfig

        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute(
            "CREATE TABLE seedbank.frame_probe "
            "(id INTEGER, low_card TIMESTAMPTZ, high_card TIMESTAMPTZ)",
        )
        fresh_duckdb.execute(
            "INSERT INTO seedbank.frame_probe SELECT i, "
            "TIMESTAMPTZ '2020-01-17 00:00:00+00' + INTERVAL (i % 3) HOUR, "
            "TIMESTAMPTZ '2020-01-17 00:00:00+00' + INTERVAL (i % 60) MINUTE "
            "FROM range(240) t(i)",
        )
        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])
        columns = adapter.introspect_columns("memory.seedbank.frame_probe")
        _, stats = adapter.compute_statistics(
            "memory.seedbank.frame_probe",
            columns,
            StatisticsConfig(enumeration_threshold=5),
            frozenset(),
        )

        low, high = stats["low_card"], stats["high_card"]

        assert low.values, "a sub-threshold TZ column must carry a value list"
        assert all(entry.value.endswith("Z") for entry in low.values), low.values
        assert high.range is not None
        assert high.range.min is not None and high.range.min.endswith("Z")


class TestApproximateCardinality:
    """Above a catalog row-count threshold the distinct counts are estimated.

    SPEC 2.2.2 sanctions HLL and requires `cardinality_method` to record which branch ran;
    the threshold decision reads the catalog rather than counting.
    """

    @staticmethod
    def _run(
        con: duckdb.DuckDBPyConnection,
        threshold: int,
    ) -> tuple[list[str], PhaseB]:
        from dbprint.config import StatisticsConfig

        recorder = _RecordingShim(con)
        adapter = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: recorder)
        adapter.connect()
        adapter.list_tables(include=["*"], exclude=[])
        columns = adapter.introspect_columns("memory.seedbank.curation_event")

        with patch.object(adapters_base, "APPROXIMATE_THRESHOLD", threshold):
            _, stats = adapter.compute_statistics(
                "memory.seedbank.curation_event",
                columns,
                StatisticsConfig(),
                frozenset(),
            )

        return recorder.statements, stats

    def test_above_the_threshold_estimates_and_says_so(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        _seed_wide(fresh_duckdb)
        statements, stats = self._run(fresh_duckdb, threshold=10)

        phase_a = [s for s in statements if "AS row_count" in s]

        assert phase_a, "phase A must have run"
        assert any("APPROX_COUNT_DISTINCT(" in s for s in phase_a), phase_a
        assert not any("COUNT(DISTINCT" in s for s in phase_a), phase_a

        # Low-cardinality columns keep the estimate; near-unique ones are re-counted exactly,
        # so noise never crosses the SPEC 4.2 candidate-key line. `seen_at` alone repeats.
        assert stats["seen_at"].cardinality_method == "approximate"
        assert stats["id"].cardinality_method == "exact"
        assert stats["label"].cardinality_method == "exact"

    def test_below_the_threshold_counts_exactly(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        _seed_wide(fresh_duckdb)
        statements, stats = self._run(fresh_duckdb, threshold=1_000_000)

        phase_a = [s for s in statements if "AS row_count" in s]

        assert any("COUNT(DISTINCT" in s for s in phase_a), phase_a
        assert not any("APPROX_COUNT_DISTINCT(" in s for s in phase_a), phase_a
        assert all(s.cardinality_method == "exact" for s in stats.values())

    def test_classification_is_stable_across_the_threshold(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """The estimated count may move; the shape it selects may not - shape is classification."""

        _seed_wide(fresh_duckdb)
        _, approximate = self._run(fresh_duckdb, threshold=10)
        _, exact = self._run(fresh_duckdb, threshold=1_000_000)

        assert approximate.keys() == exact.keys()

        def shape(s: ColumnStats) -> tuple[bool, ...]:
            return (
                s.values is not None,
                s.values_coverage is not None,
                s.range is not None,
                s.percentiles is not None,
            )

        for name, approx_stats in approximate.items():
            assert shape(approx_stats) == shape(exact[name]), name

    def test_a_near_unique_column_keeps_its_exact_count(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """The estimate must not decide the SPEC 4.2 candidate-key question by itself."""

        from dbprint.config import StatisticsConfig

        _seed_wide(fresh_duckdb)
        _, approximate = self._run(fresh_duckdb, threshold=10)
        _, exact = self._run(fresh_duckdb, threshold=1_000_000)

        assert approximate["id"].cardinality == exact["id"].cardinality
        assert approximate["id"].cardinality_ratio == exact["id"].cardinality_ratio
        # The numeric branch either way - the estimate must not decide the candidate-key ratio.
        assert (approximate["id"].cardinality_ratio or 0) >= 0.9999
        # numeric takes the top-N branch (SPEC 2.2.3), never a full categorical enumeration.
        assert approximate["id"].values is not None
        assert len(approximate["id"].values) <= StatisticsConfig().top_n_values

    def test_cardinality_never_exceeds_the_row_count(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        """HLL errs in both directions; the ratio ceiling is 1.0 per SPEC 2.2.2."""

        _seed_wide(fresh_duckdb)
        _, stats = self._run(fresh_duckdb, threshold=10)

        for name, s in stats.items():
            assert s.cardinality_ratio is not None
            assert s.cardinality_ratio <= 1.0, f"{name}: {s.cardinality_ratio}"


class _CannedPhaseARow:
    """A cursor stub returning one pre-chosen Phase A row - a real HLL estimate above the
    non-null count is not reproducible at small N, so the row is fed straight to the statement.
    """

    def __init__(self, row: tuple[int, ...]) -> None:
        self._row = row

    def execute(self, sql: str, params: object = None) -> _CannedPhaseARow:
        del sql, params

        return self

    def fetchone(self) -> tuple[int, ...]:
        return self._row

    def fetchall(self) -> list[tuple[int, ...]]:
        return [self._row]

    def close(self) -> None:
        pass


class TestApproximateEstimateBoundedByNonNullCount:
    """`cardinality` is defined over non-null values alone (SPEC 2.2.2) - the clamp must match.

    The case is an HLL estimate inside a `row_count` clamp but past the true non-null
    ceiling - invisible until the null headroom runs out.
    """

    @staticmethod
    def _identity() -> Identity:
        return Identity.of(("memory", "seedbank", "wide"), DIALECT, {"val": "VAL"})

    def test_the_phase_a_clamp(self) -> None:
        from dbprint.adapters.snowflake import stats as sf_stats

        cursor = _CannedPhaseARow((4_000_000, 1_840_000, 0, 0, 0, 2_180_000))
        col = ColumnMeta(name="val", sql_type="number", nullable=True, default=None, ordinal=1)

        _, base = sf_stats._phase_a_statement(
            cursor,
            self._identity(),
            "memory.seedbank.wide",
            [col],
            True,
        )

        assert base["val"].cardinality <= 4_000_000 - 1_840_000, base["val"].cardinality

    def test_an_estimate_within_bounds_is_untouched(self) -> None:
        from dbprint.adapters.snowflake import stats as sf_stats

        cursor = _CannedPhaseARow((4_000_000, 1_840_000, 0, 0, 0, 2_000_000))
        col = ColumnMeta(name="val", sql_type="number", nullable=True, default=None, ordinal=1)

        _, base = sf_stats._phase_a_statement(
            cursor,
            self._identity(),
            "memory.seedbank.wide",
            [col],
            True,
        )

        assert base["val"].cardinality == 2_000_000


class TestPhaseAIsIssuedInBatches:
    """A wide table's phase A exceeds what an engine will compile as one statement."""

    @staticmethod
    def _wide_table(con: duckdb.DuckDBPyConnection, columns: int) -> None:
        names = [f"c{i}" for i in range(columns)]
        con.execute(f"CREATE TABLE wide ({', '.join(f'{n} VARCHAR' for n in names)})")
        con.execute(
            f"INSERT INTO wide VALUES ({', '.join(chr(39) + 'x' + chr(39) for n in names)})",
        )

    def test_every_column_is_measured_across_the_batches(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dbprint.adapters.snowflake import stats as sf_stats
        from dbprint.config import StatisticsConfig

        self._wide_table(fresh_duckdb, columns=150)
        adapter = _build_adapter(fresh_duckdb)
        adapter.list_tables(include=["*"], exclude=[])
        statements: list[str] = []
        real = sf_stats.exec_query

        def recording(cursor: Any, sql: str) -> Any:
            if " ".join(sql.split()).startswith("SELECT COUNT(1) AS row_count"):
                statements.append(sql)

            return real(cursor, sql)

        monkeypatch.setattr(sf_stats, "exec_query", recording)
        columns = adapter.introspect_columns("memory.main.wide")
        counts, phase_a = adapter.compute_base_statistics(
            "memory.main.wide",
            columns,
            StatisticsConfig(),
        )
        base = phase_a.stats

        assert len(statements) > 1
        assert counts.row_count == 1
        assert {name for name, stats in base.items() if stats.cardinality is None} == set()
        assert sorted(base) == sorted(c.name for c in columns)


class TestTableKinds:
    """Each `table_type` Snowflake reports is listed, skipped or retyped on purpose (SPEC 2.5)."""

    def _adapter(
        self,
        con: duckdb.DuckDBPyConnection,
        table_types: dict[str, str],
    ) -> tuple[SnowflakeAdapter, _RecordingShim]:
        con.execute("CREATE SCHEMA seedbank")
        con.execute("CREATE TABLE seedbank.curation_event (id INTEGER)")
        con.execute("CREATE TABLE seedbank.field_log (id INTEGER)")
        shim = _RecordingShim(con, table_types)
        adapter = SnowflakeAdapter(SNOWFLAKE_CREDS, cursor_factory=lambda _: shim)
        adapter.connect()

        return adapter, shim

    def test_an_event_table_is_listed_as_a_table(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        adapter, _shim = self._adapter(fresh_duckdb, {"field_log": "EVENT TABLE"})
        listed = {m.fqn: m.type for m in adapter.list_tables(include=["*"], exclude=[])}

        assert listed["memory.seedbank.field_log"] == "table"
        assert listed["memory.seedbank.curation_event"] == "table"

    def test_an_external_table_is_a_table_marked_external(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        adapter, _shim = self._adapter(fresh_duckdb, {"field_log": "EXTERNAL TABLE"})
        listed = {m.fqn: m for m in adapter.list_tables(include=["*"], exclude=[])}

        assert listed["memory.seedbank.field_log"].type == "table"
        assert listed["memory.seedbank.field_log"].external is True
        assert listed["memory.seedbank.curation_event"].external is False

    def test_an_event_table_asks_get_ddl_for_its_own_kind(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        adapter, shim = self._adapter(fresh_duckdb, {"field_log": "EVENT TABLE"})
        adapter.list_tables(include=["*"], exclude=[])

        ddl = adapter.extract_ddl("memory.seedbank.field_log")
        adapter.extract_ddl("memory.seedbank.curation_event")
        sent = [s for s in shim.statements if "GET_DDL(" in s]

        assert "CREATE TABLE" in ddl
        assert sent[0].startswith("SELECT GET_DDL('EVENT_TABLE', ")
        assert sent[1].startswith("SELECT GET_DDL('TABLE', ")

    def test_a_temporary_table_is_skipped_by_name_not_by_omission(self) -> None:
        assert "TEMPORARY TABLE" in snowflake_introspect._UNLISTED_TABLE_TYPES
        assert "TEMPORARY TABLE" not in snowflake_introspect._TABLE_TYPE_MAP

    def test_a_temporary_table_is_not_listed(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        adapter, _shim = self._adapter(fresh_duckdb, {"field_log": "TEMPORARY TABLE"})
        listed = {m.fqn for m in adapter.list_tables(include=["*"], exclude=[])}

        assert listed == {"memory.seedbank.curation_event"}

    def test_a_sampled_run_lists_no_sample_copy(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
        tmp_path: Path,
    ) -> None:
        fresh_duckdb.execute("CREATE SCHEMA seedbank")
        fresh_duckdb.execute(
            "CREATE TABLE seedbank.field_log AS SELECT range AS id FROM range(200)",
        )
        adapter = SnowflakeAdapter(
            SNOWFLAKE_CREDS,
            cursor_factory=lambda _: SnowflakeDialectShim(fresh_duckdb),
        )
        adapter.connect()
        config = connection_config(
            adapter="snowflake",
            output=tmp_path,
            rules=(RuleConfig(include=("*.field_log",), sample=0.5),),
        )

        try:
            Engine(adapter, config, tmp_path).generate()
        finally:
            adapter.close()

        manifest = yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())

        assert list(manifest["tables"]) == ["memory.seedbank.field_log"]
