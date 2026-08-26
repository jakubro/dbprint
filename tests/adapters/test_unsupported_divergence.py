"""A type the adapter cannot profile but the format does not name.

`dbprint.spec` lists the types the format can describe and an adapter lists its vendor's, so
the second is the larger set. Without `supported=False`, phase A classifies such a column
`text` while phase B returns `cardinality: null` - a schema violation. Only MySQL and
Snowflake can reach it; Postgres's unsupported list is a subset of the format's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from dbprint.adapters import Adapter, AdapterType
from dbprint.adapters.mysql.stats import _UNSUPPORTED_TYPES as MYSQL_UNSUPPORTED
from dbprint.adapters.postgres.stats import _UNSUPPORTED_TYPES as POSTGRES_UNSUPPORTED
from dbprint.adapters.snowflake.stats import _UNSUPPORTED_TYPES as SNOWFLAKE_UNSUPPORTED
from dbprint.config.project import ConnectionConfig, DiffConfig, StatisticsConfig
from dbprint.conformance import validate_print
from dbprint.engine import Engine
from dbprint.spec.classification import _UNSUPPORTED_TYPES as SPEC_UNSUPPORTED
from dbprint.spec.classification import classify
from tests.adapters.test_mysql import _build as build_mysql
from tests.adapters.test_snowflake import _build_adapter as build_snowflake


VENDOR_ONLY: dict[str, frozenset[str]] = {
    "mysql": frozenset(MYSQL_UNSUPPORTED) - frozenset(SPEC_UNSUPPORTED),
    "snowflake": frozenset(SNOWFLAKE_UNSUPPORTED) - frozenset(SPEC_UNSUPPORTED),
    "postgres": frozenset(POSTGRES_UNSUPPORTED) - frozenset(SPEC_UNSUPPORTED),
}


class TestTheDivergenceIsReal:
    """Pure: the lists themselves, with no database in the way."""

    @pytest.mark.parametrize("vendor", ["mysql", "snowflake"])
    def test_the_adapter_knows_types_the_format_does_not(self, vendor: str) -> None:
        """The precondition: an empty result would make the divergence below untestable."""

        assert VENDOR_ONLY[vendor], f"{vendor} no longer carries a type outside the spec list"

    def test_postgres_cannot_exhibit_the_case(self) -> None:
        """Which is exactly why a Postgres-only run cannot see this class of defect."""

        assert VENDOR_ONLY["postgres"] == frozenset()

    @pytest.mark.parametrize("vendor", ["mysql", "snowflake"])
    def test_a_measured_cardinality_would_misclassify_every_one_of_them(
        self,
        vendor: str,
    ) -> None:
        """The counterfactual: what the engine would do if it read Phase A's count."""

        misclassified = {
            sql_type: classify(sql_type, 1000, False, 50) for sql_type in VENDOR_ONLY[vendor]
        }

        assert all(v != "unsupported" for v in misclassified.values()), misclassified

    @pytest.mark.parametrize("vendor", ["mysql", "snowflake"])
    def test_withholding_the_cardinality_classifies_them_unsupported(self, vendor: str) -> None:
        """And what it does once the adapter says it could not profile the column."""

        verdicts = {
            sql_type: classify(sql_type, None, False, 50) for sql_type in VENDOR_ONLY[vendor]
        }

        assert set(verdicts.values()) == {"unsupported"}, verdicts


# Spelled directly rather than seeded live: MariaDB rescues `unsigned` with its own `(N)`
# display width, so a live MySQL fixture would pass for the wrong reason, and none of the
# Postgres types are in `POSTGRES_UNSUPPORTED` to seed either.
_MYSQL_8_UNSIGNED_SPELLING = "bigint unsigned"
_POSTGRES_NETWORK_AND_TEXT_FAMILY = (
    "inet",
    "cidr",
    "macaddr",
    "xml",
    "interval",
    "bit varying",
    "tsvector",
)


class TestATypeNoListNamesClassifiesByMeasurement:
    """The two instances beyond `VENDOR_ONLY`: a spelling divergence, and a closed-list gap."""

    def test_mysql_8_reports_no_display_width_and_still_classifies_numeric(self) -> None:
        """MySQL 8.0.19+ drops the display width MariaDB still reports."""

        result = classify(_MYSQL_8_UNSIGNED_SPELLING, 1000, False, 50)
        assert result == "numeric"

    def test_the_postgres_network_and_text_family_classifies_by_measurement(self) -> None:
        """None of these are in `POSTGRES_UNSUPPORTED`; a measured one is `text`."""

        verdicts = {
            sql_type: classify(sql_type, 1000, False, 50)
            for sql_type in _POSTGRES_NETWORK_AND_TEXT_FAMILY
        }

        assert set(verdicts.values()) == {"text"}, verdicts

    def test_the_same_family_stays_unsupported_when_the_adapter_declines_it(self) -> None:
        verdicts = {
            sql_type: classify(sql_type, None, False, 50)
            for sql_type in _POSTGRES_NETWORK_AND_TEXT_FAMILY
        }

        assert set(verdicts.values()) == {"unsupported"}, verdicts


class TestMysqlReportsItsOwnUnsupportedTypes:
    """`longblob`: MariaDB has it, the format does not name it."""

    def test_phase_a_says_it_could_not_profile_the_column(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        _seed_mysql(mysql_test_db)
        adapter = build_mysql(mysql_test_db)

        try:
            columns = adapter.introspect_columns(f"{mysql_test_db['database']}.blobs")
            _, base = adapter.compute_base_statistics(
                f"{mysql_test_db['database']}.blobs",
                columns,
                StatisticsConfig(),
            )
        finally:
            adapter.close()

        assert base["payload"].supported is False
        assert base["label"].supported is True

    def test_the_column_classifies_unsupported_and_the_print_validates(
        self,
        mysql_test_db: dict[str, str],
        tmp_path: Path,
    ) -> None:
        _seed_mysql(mysql_test_db)
        payload = _generate(build_mysql(mysql_test_db), "mysql", tmp_path, "*.blobs")
        columns = payload["columns"]

        assert columns["payload"]["classification"] == "unsupported"
        assert columns["label"]["classification"] != "unsupported"
        _assert_conformant(tmp_path)


class TestSnowflakeReportsItsOwnUnsupportedTypes:
    """`geometry`: the substrate has it, the format does not name it."""

    def test_phase_a_says_it_could_not_profile_the_column(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
    ) -> None:
        _seed_snowflake(fresh_duckdb)
        adapter = build_snowflake(fresh_duckdb)

        try:
            adapter.list_tables(include=["*"], exclude=[])
            columns = adapter.introspect_columns("memory.seedbank.shapes")
            _, base = adapter.compute_base_statistics(
                "memory.seedbank.shapes",
                columns,
                StatisticsConfig(),
            )
        finally:
            adapter.close()

        assert base["field_notes"].supported is False
        assert base["label"].supported is True

    def test_the_column_classifies_unsupported_and_the_print_validates(
        self,
        fresh_duckdb: duckdb.DuckDBPyConnection,
        tmp_path: Path,
    ) -> None:
        _seed_snowflake(fresh_duckdb)
        payload = _generate(build_snowflake(fresh_duckdb), "snowflake", tmp_path, "*.shapes")
        columns = payload["columns"]

        assert columns["field_notes"]["classification"] == "unsupported"
        assert columns["label"]["classification"] != "unsupported"
        _assert_conformant(tmp_path)


@pytest.fixture(name="fresh_duckdb")
def _fresh_duckdb() -> duckdb.DuckDBPyConnection:
    """Per-test in-memory duckdb: these tests need a bare connection to seed GEOMETRY into."""

    con = duckdb.connect(":memory:")
    con.execute("SET threads = 1")

    return con


def _seed_mysql(creds: dict[str, str]) -> None:
    """A long-blob column beside an ordinary one, both near-unique - a measured cardinality
    would classify the blob `text`, not `unsupported`.
    """

    import mysql.connector

    conn = mysql.connector.connect(
        host=creds["host"],
        port=int(creds["port"]),
        user=creds["user"],
        password="",
        database=creds["database"],
        autocommit=True,
    )

    try:
        cursor = conn.cursor(buffered=True)
        cursor.execute("CREATE TABLE blobs (payload LONGBLOB, label VARCHAR(64))")
        values = ",".join(f"(UNHEX('{i:064x}'), 'label_{i % 4}')" for i in range(60))
        cursor.execute(f"INSERT INTO blobs (payload, label) VALUES {values}")
        cursor.close()
    finally:
        conn.close()


def _seed_snowflake(conn: duckdb.DuckDBPyConnection) -> None:
    """A GEOMETRY column beside an ordinary one, NULL-valued: duckdb's spatial extension is
    not loaded, and the declared type alone reaches the zero-cardinality divergence branch.
    """

    conn.execute("CREATE SCHEMA IF NOT EXISTS seedbank")
    conn.execute("CREATE TABLE seedbank.shapes (field_notes GEOMETRY, label VARCHAR)")
    conn.execute("INSERT INTO seedbank.shapes SELECT NULL, 'label_' || (i % 4) FROM range(60) t(i)")


def _generate(
    adapter: Adapter,
    name: AdapterType,
    tmp_path: Path,
    include: str,
) -> dict[str, Any]:
    conn_config = ConnectionConfig(
        name="primary",
        adapter=name,
        auto=True,
        output=tmp_path,
        include=(include,),
        exclude=(),
        max_age_days=7,
        statistics=StatisticsConfig(),
        diff=DiffConfig(),
    )

    try:
        Engine(adapter, conn_config, tmp_path).generate()
    finally:
        adapter.close()

    written = list((tmp_path / "primary").rglob("statistics.yaml"))

    assert len(written) == 1, f"expected one profiled table, got {written}"

    return yaml.safe_load(written[0].read_text())


def _assert_conformant(tmp_path: Path) -> None:
    errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]

    assert errors == [], "\n".join(f"  {e.code} at {e.path}: {e.detail}" for e in errors)
