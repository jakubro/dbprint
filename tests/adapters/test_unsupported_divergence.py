"""A type the adapter cannot profile but the format does not name.

Without `supported=False`, phase A would classify it `text` while phase B publishes no cardinality.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from dbprint.adapters import Adapter, AdapterType, PostgresAdapter
from dbprint.adapters.postgres.stats import _UNSUPPORTED_TYPES as POSTGRES_UNSUPPORTED
from dbprint.adapters.snowflake.stats import _UNSUPPORTED_TYPES as SNOWFLAKE_UNSUPPORTED
from dbprint.config.project import ConnectionConfig, DiffConfig, StatisticsConfig
from dbprint.conformance import validate_print
from dbprint.engine import Engine
from dbprint.spec.classification import _UNSUPPORTED_TYPES as SPEC_UNSUPPORTED
from dbprint.spec.classification import classify


# Types each adapter declines to profile that the format's own unsupported list does not name.
VENDOR_ONLY: dict[str, frozenset[str]] = {
    "snowflake": frozenset({"file", "unknown"}),
    "postgres": frozenset(
        {
            "aclitem",
            "box",
            "cid",
            "circle",
            "geometric",
            "gtsvector",
            "line",
            "lseg",
            "path",
            "pg_snapshot",
            "refcursor",
            "txid_snapshot",
            "xid",
            "xml",
        },
    ),
}

_ADAPTER_UNSUPPORTED = {
    "snowflake": SNOWFLAKE_UNSUPPORTED,
    "postgres": POSTGRES_UNSUPPORTED,
}


class TestTheDivergenceIsReal:
    """Pure: the lists themselves, with no database in the way."""

    @pytest.mark.parametrize("vendor", ["snowflake", "postgres"])
    def test_the_adapter_knows_types_the_format_does_not(self, vendor: str) -> None:
        """The precondition: every named type is the adapter's to decline and not the format's."""

        assert VENDOR_ONLY[vendor] <= frozenset(_ADAPTER_UNSUPPORTED[vendor])
        assert not VENDOR_ONLY[vendor] & frozenset(SPEC_UNSUPPORTED)

    @pytest.mark.parametrize("vendor", ["snowflake", "postgres"])
    def test_a_measured_cardinality_would_misclassify_every_one_of_them(
        self,
        vendor: str,
    ) -> None:
        """The counterfactual: what the engine would do if it read Phase A's count."""

        misclassified = {
            sql_type: classify(sql_type, 1000, False, 50) for sql_type in VENDOR_ONLY[vendor]
        }

        assert all(v != "unsupported" for v in misclassified.values()), misclassified

    @pytest.mark.parametrize("vendor", ["snowflake", "postgres"])
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


class TestPostgresReportsItsOwnGeometricTypes:
    """`point` and `box`: the substrate has them, the format names neither (they are not OGC)."""

    def test_phase_a_says_it_could_not_profile_the_column(
        self,
        postgres_test_db: dict[str, str],
    ) -> None:
        _seed_postgres(postgres_test_db)
        adapter = PostgresAdapter(postgres_test_db)
        adapter.connect()

        try:
            fqn = next(t.fqn for t in adapter.list_tables(include=["*"], exclude=[]))
            columns = adapter.introspect_columns(fqn)
            _, phase_a = adapter.compute_base_statistics(fqn, columns, StatisticsConfig())
            base = phase_a.stats
        finally:
            adapter.close()

        assert base["spot"].supported is False
        assert base["frame"].supported is False
        assert base["label"].supported is True

    def test_the_column_classifies_unsupported_and_the_print_validates(
        self,
        postgres_test_db: dict[str, str],
        tmp_path: Path,
    ) -> None:
        _seed_postgres(postgres_test_db)
        payload = _generate(PostgresAdapter(postgres_test_db), "postgres", tmp_path, "*.shapes")
        columns = payload["columns"]

        assert columns["spot"]["classification"] == "unsupported"
        assert columns["frame"]["classification"] == "unsupported"
        assert columns["label"]["classification"] != "unsupported"
        _assert_conformant(tmp_path)


def _seed_postgres(creds: dict[str, str]) -> None:
    """Near-unique geometric columns beside an ordinary one - a measured cardinality would
    classify them `spatial` by name, not `unsupported`.
    """

    with psycopg.connect(
        host=creds["host"],
        port=int(creds["port"]),
        dbname=creds["database"],
        user=creds["user"],
        password="",
        autocommit=True,
    ) as conn:
        conn.execute("CREATE TABLE public.shapes (spot point, frame box, label text)")
        conn.execute(
            "INSERT INTO public.shapes SELECT point(i, i), box(point(0, 0), point(i, i)), "
            "'label_' || (i % 4) FROM generate_series(1, 60) i",
        )


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
