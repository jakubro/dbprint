"""MysqlAdapter-specific behaviors not covered by the contract suite.

Live tests run against the ephemeral MariaDB cluster; the rest are server-free units. MariaDB
renders JSON as longtext, so the native JSON type is covered only by the gated live suite.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml
from click.testing import CliRunner

from dbprint.adapters import MysqlAdapter, TableScope
from dbprint.adapters.base import ColumnMeta, ColumnStats, UniqueKeyMeta
from dbprint.adapters.errors import QueryFailed
from dbprint.adapters.mysql import ddl as ddl_module
from dbprint.adapters.mysql import introspect as introspect_module
from dbprint.adapters.mysql import stats as stats_module
from dbprint.adapters.mysql.connection import ConnectionParams, MysqlConnectionError, exec_query
from dbprint.cli.main import main
from dbprint.config import StatisticsConfig
from dbprint.conformance import validate_print
from tests.conftest import MysqlCluster


CREDS: dict[str, str] = {
    "host": "127.0.0.1",
    "port": "3306",
    "database": "app",
    "user": "root",
    "password": "",
}

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mysql"


def _column(sql_type: str, name: str = "c") -> ColumnMeta:
    return ColumnMeta(name=name, sql_type=sql_type, nullable=True, default=None, ordinal=1)


class _StubCursor:
    """Canned-row cursor: exec_query just calls execute() and returns the cursor."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, sql: str, params: object = None) -> None:
        return None

    def fetchall(self) -> list[tuple]:
        return self._rows

    def fetchone(self) -> object:
        return self._rows[0] if self._rows else None

    def close(self) -> None:
        pass


class TestConnectionParams:
    def test_required_keys_enumerated(self) -> None:
        assert set(MysqlAdapter.REQUIRED_KEYS) == {
            "host",
            "port",
            "database",
            "user",
            "password",
        }

    def test_missing_credential_key_raises(self) -> None:
        incomplete = {k: v for k, v in CREDS.items() if k != "database"}

        with pytest.raises(MysqlConnectionError, match="database"):
            ConnectionParams.from_credentials(incomplete)

    def test_invalid_port_raises(self) -> None:
        with pytest.raises(MysqlConnectionError, match="invalid port"):
            ConnectionParams.from_credentials({**CREDS, "port": "not-a-port"})


class TestDdlNormalization:
    _RAW = (
        "CREATE TABLE `herbarium_sheet` (\n"
        "  `id` int(11) NOT NULL AUTO_INCREMENT,\n"
        "  `label` varchar(64) NOT NULL,\n"
        "  PRIMARY KEY (`id`)\n"
        ") ENGINE=InnoDB AUTO_INCREMENT=42 DEFAULT CHARSET=latin1"
    )

    def test_table_option_counter_stripped(self) -> None:
        out = ddl_module.normalize(self._RAW)
        assert "AUTO_INCREMENT=42" not in out
        assert "AUTO_INCREMENT=" not in out

    def test_column_keyword_preserved(self) -> None:
        out = ddl_module.normalize(self._RAW)
        assert "int(11) NOT NULL AUTO_INCREMENT" in out

    def test_trailing_newline_ensured(self) -> None:
        assert ddl_module.normalize(self._RAW).endswith("\n")


class _TraceStubCursor:
    """Stub cursor whose `execute` succeeds and reports a fixed rowcount."""

    def __init__(self, rowcount: int = 3) -> None:
        self.rowcount = rowcount

    def execute(self, sql: str, params: Any = None) -> None:
        del sql, params

    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> Any:
        return None

    def close(self) -> None:
        pass


class _TraceRaisingCursor:
    """Stub cursor whose `execute` always raises - proves the seam wraps the failure."""

    def execute(self, sql: str, params: Any = None) -> None:
        del sql, params
        raise RuntimeError("boom")

    def fetchall(self) -> list[Any]:
        return []

    def fetchone(self) -> Any:
        return None

    def close(self) -> None:
        pass


class TestStatementTrace:
    """exec_query's own DEBUG record - statement, params, elapsed, rows."""

    def test_success_logs_statement_params_and_rows(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level(logging.DEBUG, logger="dbprint.adapters.mysql.connection"):
            exec_query(_TraceStubCursor(3), "SELECT %s", ("x",))

        assert "SELECT %s" in caplog.text
        assert "rows=3" in caplog.text

    def test_failure_logs_before_the_exception_propagates(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            caplog.at_level(logging.DEBUG, logger="dbprint.adapters.mysql.connection"),
            pytest.raises(QueryFailed),
        ):
            exec_query(_TraceRaisingCursor(), "SELECT 1")

        assert "statement failed" in caplog.text


class TestExecuteQueryTrace:
    """The SQL-assertion seam (`check --online`'s `sql:` block) is traced like any statement."""

    def test_the_operators_statement_is_traced(
        self,
        mysql_test_db: dict[str, str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        adapter = _build(mysql_test_db)

        try:
            with caplog.at_level(logging.DEBUG, logger="dbprint.adapters.mysql.connection"):
                adapter.execute_query("SELECT 1 AS n")
        finally:
            adapter.close()

        assert "SELECT 1 AS n" in caplog.text
        assert "rows=1" in caplog.text


class TestClassificationDispatch:
    """The adapter's pre-classification steers Phase B; it mirrors SPEC 3.2."""

    def _pre(self, sql_type: str, cardinality: int, fk: bool = False) -> str:
        return stats_module._pre_classify(
            _column(sql_type),
            cardinality,
            StatisticsConfig(),
            fk,
        )

    def test_json_dispatches_to_json(self) -> None:
        assert self._pre("json", cardinality=900) == "json"

    def test_enum_low_cardinality_is_categorical(self) -> None:
        assert self._pre("enum('a','b','c')", cardinality=3) == "categorical"

    def test_high_cardinality_int_is_numeric(self) -> None:
        assert self._pre("int(11)", cardinality=100000) == "numeric"

    def test_unsigned_int_is_numeric(self) -> None:
        assert self._pre("int(10) unsigned", cardinality=100000) == "numeric"

    def test_datetime_is_temporal(self) -> None:
        assert self._pre("datetime", cardinality=100000) == "temporal"

    def test_blob_is_unsupported(self) -> None:
        assert self._pre("blob", cardinality=5) == "unsupported"

    def test_full_cardinality_is_text(self) -> None:
        """Uniqueness is not a classification (SPEC 4.2) - a unique char column stays text."""

        assert self._pre("char(36)", cardinality=1000) == "text"


class TestIdentifierNormalization:
    def test_backticks_stripped_and_lowercased(self) -> None:
        assert introspect_module._norm("`MixedCase`") == "mixedcase"

    def test_plain_identifier_lowercased(self) -> None:
        assert introspect_module._norm("Herbarium") == "herbarium"


class TestPhysicalColumnIdentity:
    """SPEC 2.2.4: MySQL folds column names case-insensitively, but the catalog keeps the
    declared spelling - carried as `physical_name` so detection (SPEC 4.4.3) reads it.
    """

    def _seed_mixed_case(self, creds: dict[str, str]) -> None:
        import mysql.connector

        conn = mysql.connector.connect(
            host=creds["host"],
            port=int(creds["port"]),
            user=creds["user"],
            password=creds["password"],
            database=creds["database"],
            autocommit=True,
        )
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE curator_profile (id INT PRIMARY KEY AUTO_INCREMENT, fullName VARCHAR(50))",
        )
        cur.executemany(
            "INSERT INTO curator_profile (fullName) VALUES (%s)",
            [(f"name-{i % 55}",) for i in range(200)],
        )
        cur.close()
        conn.close()

    def test_the_map_key_is_lowercase_and_the_physical_spelling_is_carried(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        self._seed_mixed_case(mysql_test_db)
        adapter = _build(mysql_test_db)

        try:
            fqn = f"{mysql_test_db['database']}.curator_profile"
            cols = {c.name: c for c in adapter.introspect_columns(fqn)}
        finally:
            adapter.close()

        assert cols["fullname"].physical_name == "fullName"
        assert cols["id"].physical_name is None

    def test_sensitivity_detects_the_camel_case_column(
        self,
        mysql_test_db: dict[str, str],
        tmp_path: Path,
    ) -> None:
        from dbprint.config.project import ConnectionConfig, DiffConfig
        from dbprint.engine import Engine

        self._seed_mixed_case(mysql_test_db)
        fqn = f"{mysql_test_db['database']}.curator_profile"
        conn = ConnectionConfig(
            name="primary",
            adapter="mysql",
            auto=False,
            output=tmp_path,
            include=(fqn,),
            exclude=(),
            max_age_days=7,
            statistics=StatisticsConfig(),
            diff=DiffConfig(),
        )
        Engine(MysqlAdapter(mysql_test_db), conn, tmp_path).generate()

        table_dir = tmp_path / "primary" / mysql_test_db["database"] / "curator_profile"
        stats = yaml.safe_load((table_dir / "statistics.yaml").read_text())
        column = stats["columns"]["fullname"]

        assert column["physical_name"] == "fullName"
        assert column["inferred"]["sensitivity"] == "personal_name"

        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )


class TestCollation:
    """SPEC 2.2.2/2.2.4: `cardinality` is collation-relative, so the connection default is
    recorded once and a column only where it overrides. Unlike Postgres,
    `information_schema.columns.collation_name` is always populated here.
    """

    def _seed(self, creds: dict[str, str]) -> None:
        import mysql.connector

        conn = mysql.connector.connect(
            host=creds["host"],
            port=int(creds["port"]),
            user=creds["user"],
            password=creds["password"],
            database=creds["database"],
            autocommit=True,
        )
        cur = conn.cursor()
        cur.execute(
            "CREATE TABLE labels (id INT PRIMARY KEY AUTO_INCREMENT, "
            "plain VARCHAR(20), forced VARCHAR(20) COLLATE utf8mb4_bin)",
        )
        cur.executemany(
            "INSERT INTO labels (plain, forced) VALUES (%s, %s)",
            [(f"v{i % 5}", f"v{i % 5}") for i in range(50)],
        )
        cur.close()
        conn.close()

    def test_introspection_carries_the_override_and_omits_the_default(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        self._seed(mysql_test_db)
        adapter = _build(mysql_test_db)

        try:
            fqn = f"{mysql_test_db['database']}.labels"
            cols = {c.name: c for c in adapter.introspect_columns(fqn)}
            default = adapter.default_collation()
        finally:
            adapter.close()

        assert cols["forced"].collation == "utf8mb4_bin"
        assert cols["plain"].collation == default
        assert default != "utf8mb4_bin"

    def test_generate_emits_collation_only_where_it_overrides_the_connection_default(
        self,
        mysql_test_db: dict[str, str],
        tmp_path: Path,
    ) -> None:
        from dbprint.config.project import ConnectionConfig, DiffConfig
        from dbprint.engine import Engine

        self._seed(mysql_test_db)
        fqn = f"{mysql_test_db['database']}.labels"
        conn = ConnectionConfig(
            name="primary",
            adapter="mysql",
            auto=False,
            output=tmp_path,
            include=(fqn,),
            exclude=(),
            max_age_days=7,
            statistics=StatisticsConfig(),
            diff=DiffConfig(),
        )
        Engine(MysqlAdapter(mysql_test_db), conn, tmp_path).generate()

        manifest = yaml.safe_load((tmp_path / "primary" / "manifest.yaml").read_text())
        table_dir = tmp_path / "primary" / mysql_test_db["database"] / "labels"
        stats = yaml.safe_load((table_dir / "statistics.yaml").read_text())
        columns = stats["columns"]

        assert manifest["default_collation"] and manifest["default_collation"] != "utf8mb4_bin"
        assert "collation" not in columns["plain"]
        assert columns["forced"]["collation"] == "utf8mb4_bin"

        errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )

    def test_a_case_variant_pair_diverges_only_under_the_case_sensitive_column(
        self,
        mysql_test_db: dict[str, str],
        tmp_path: Path,
    ) -> None:
        """Two rows collapse to one distinct value under the case-insensitive default and
        stay two under an explicit override - `cardinality` depends on the collation.
        """

        import mysql.connector

        from dbprint.config.project import ConnectionConfig, DiffConfig
        from dbprint.engine import Engine

        conn_db = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )
        cur = conn_db.cursor()
        cur.execute(
            "CREATE TABLE case_variants (id INT PRIMARY KEY AUTO_INCREMENT, "
            "plain VARCHAR(20), forced VARCHAR(20) COLLATE utf8mb4_bin)",
        )
        cur.executemany(
            "INSERT INTO case_variants (plain, forced) VALUES (%s, %s)",
            [("USA", "USA"), ("usa", "usa")],
        )
        cur.close()
        conn_db.close()

        fqn = f"{mysql_test_db['database']}.case_variants"
        conn = ConnectionConfig(
            name="primary",
            adapter="mysql",
            auto=False,
            output=tmp_path,
            include=(fqn,),
            exclude=(),
            max_age_days=7,
            statistics=StatisticsConfig(),
            diff=DiffConfig(),
        )
        Engine(MysqlAdapter(mysql_test_db), conn, tmp_path).generate()

        table_dir = tmp_path / "primary" / mysql_test_db["database"] / "case_variants"
        columns = yaml.safe_load((table_dir / "statistics.yaml").read_text())["columns"]

        assert columns["plain"]["cardinality"] == 1
        assert columns["forced"]["cardinality"] == 2


class TestIdentifierRejection:
    """SPEC 1.5: producers reject identifiers that violate the path-segment allowlist.

    The `zz_` prefix isolates these tables via `include` from the contract schema
    `mysql_test_db` seeds into the same database.
    """

    def test_unsafe_character_rejected(self, mysql_test_db: dict[str, str]) -> None:
        import mysql.connector

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE `zz_weird name` (id INT)")
            cursor.close()
        finally:
            conn.close()

        adapter = _build(mysql_test_db)

        try:
            with pytest.raises(introspect_module.IdentifierRejected) as exc_info:
                adapter.list_tables(include=[f"{mysql_test_db['database']}.zz_*"], exclude=[])

            message = str(exc_info.value)
            assert "contains-unsafe-character" in message
            assert "Resolution:" in message
            assert "exclude:" in message
        finally:
            adapter.close()

    def test_excluded_unsafe_identifier_does_not_block(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        """Per SPEC 1.5.5: excluding the bad table via selectors lets the run proceed."""

        import mysql.connector

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE `zz_weird name` (id INT)")
            cursor.execute("CREATE TABLE zz_ok (id INT)")
            cursor.close()
        finally:
            conn.close()

        adapter = _build(mysql_test_db)
        db = mysql_test_db["database"]

        try:
            tables = adapter.list_tables(
                include=[f"{db}.zz_*"],
                exclude=[f"{db}.zz_weird name"],
            )
            fqns = {t.fqn for t in tables}
            assert f"{db}.zz_ok" in fqns
            assert f"{db}.zz_weird name" not in fqns
        finally:
            adapter.close()

    def test_case_collision_rejected(self, mysql_test_db: dict[str, str]) -> None:
        """SPEC 1.5.2: two identifiers that lowercase to the same path abort the run.

        Requires `lower_case_table_names=0` (the Linux default this cluster inherits); under
        `1` the pair could not exist, since names are folded at creation.
        """

        import mysql.connector

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE `zz_Curator` (id INT)")
            cursor.execute("CREATE TABLE `zz_curator` (id INT)")
            cursor.close()
        finally:
            conn.close()

        adapter = _build(mysql_test_db)
        db = mysql_test_db["database"]

        try:
            with pytest.raises(introspect_module.IdentifierRejected) as exc_info:
                adapter.list_tables(include=[f"{db}.zz_*"], exclude=[])

            message = str(exc_info.value)
            # Either row can be the "previous" entry - catalog order is a MariaDB detail -
            # so assert both names appear without fixing which is which.
            assert "case-collides-with-" in message
            assert f"{db}.zz_Curator" in message
            assert f"{db}.zz_curator" in message
        finally:
            adapter.close()

    def test_excluded_case_collision_lets_the_run_proceed(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        """Per SPEC 1.5.4: excluding the pair's shared path resolves the collision.

        Selectors match the lowercased FQN, so excluding it drops both candidates rather
        than picking a survivor; the unrelated table proves the run still completes.
        """

        import mysql.connector

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE `zz_Curator` (id INT)")
            cursor.execute("CREATE TABLE `zz_curator` (id INT)")
            cursor.execute("CREATE TABLE zz_ok (id INT)")
            cursor.close()
        finally:
            conn.close()

        adapter = _build(mysql_test_db)
        db = mysql_test_db["database"]

        try:
            tables = adapter.list_tables(
                include=[f"{db}.zz_*"],
                exclude=[f"{db}.zz_curator"],
            )
            assert [t.fqn for t in tables] == [f"{db}.zz_ok"]
        finally:
            adapter.close()


class TestIndexesFunctionalKeyParts:
    """Functional indexes emit STATISTICS rows with COLUMN_NAME=NULL; MariaDB cannot create one."""

    # (index_name, column_name, non_unique, index_type, seq_in_index)
    _ROWS: ClassVar[list[tuple]] = [
        ("t_label_idx", "label", 1, "BTREE", 1),
        ("t_func_idx", None, 1, "BTREE", 1),
        ("t_mixed_idx", "a", 1, "BTREE", 1),
        ("t_mixed_idx", None, 1, "BTREE", 2),
    ]

    def _indexes(self) -> list:
        return introspect_module.indexes(_StubCursor(self._ROWS), "fixture.t")

    def test_purely_expression_index_omitted(self) -> None:
        names = {idx.name for idx in self._indexes()}
        assert "t_func_idx" not in names

    def test_mixed_index_keeps_only_plain_columns(self) -> None:
        idx = next(i for i in self._indexes() if i.name == "t_mixed_idx")
        assert idx.columns == ("a",)

    def test_plain_secondary_index_unaffected(self) -> None:
        idx = next(i for i in self._indexes() if i.name == "t_label_idx")
        assert idx.columns == ("label",)


class TestUniqueKeysFunctionalKeyParts:
    """A unique functional index emits the same NULL COLUMN_NAME row shape.

    The declared-unique query runs the inverse filter over the same view, and the MariaDB
    substrate cannot create one, so the rows are canned.
    """

    # (index_name, column_name, seq_in_index)
    _ROWS: ClassVar[list[tuple]] = [
        ("PRIMARY", "id", 1),
        ("uq_plain", "email", 1),
        ("uq_func", None, 1),
        ("uq_mixed", "herbarium_id", 1),
        ("uq_mixed", None, 2),
    ]

    def _groups(self) -> list[tuple[str, ...]]:
        return [
            group.columns
            for group in introspect_module.unique_keys(_StubCursor(self._ROWS), "fixture.t")
        ]

    def test_a_purely_expression_index_reports_no_group(self) -> None:
        """Reporting an empty group would assert uniqueness over no column.

        The exact ordered list also settles four narrower claims: every reported column is
        real, the mixed index keeps its plain key part, the ordinary unique index is
        unaffected, and the primary key is reported first.
        """

        assert self._groups() == [("id",), ("herbarium_id",), ("email",)]


class TestLifecycle:
    def test_close_before_connect_noop(self) -> None:
        MysqlAdapter(CREDS).close()

    def test_close_twice_is_idempotent(self) -> None:
        adapter = MysqlAdapter(CREDS)
        adapter.close()
        adapter.close()


# Live MariaDB tests.


@pytest.fixture
def herbarium_sheet_db(mysql_cluster: MysqlCluster) -> Iterator[dict[str, str]]:
    """Fresh database seeded from the herbarium_sheet fixture SQL."""

    import mysql.connector

    db_name = "herbarium_catalog"
    schema = (_FIXTURE_DIR / "schema.sql").read_text()
    data = (_FIXTURE_DIR / "data.sql").read_text()

    admin = mysql.connector.connect(
        host="127.0.0.1",
        port=mysql_cluster.port,
        user="root",
        password="",
        autocommit=True,
    )
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    cur.execute(f"CREATE DATABASE `{db_name}`")
    cur.close()
    admin.close()

    seeded = mysql.connector.connect(
        host="127.0.0.1",
        port=mysql_cluster.port,
        user="root",
        password="",
        database=db_name,
        autocommit=True,
    )
    cur = seeded.cursor()

    for statement in _split_sql(schema) + _split_sql(data):
        cur.execute(statement)

    cur.close()
    seeded.close()

    creds = {
        "host": "127.0.0.1",
        "port": str(mysql_cluster.port),
        "database": db_name,
        "user": "root",
        "password": "",
    }

    try:
        yield creds
    finally:
        admin = mysql.connector.connect(
            host="127.0.0.1",
            port=mysql_cluster.port,
            user="root",
            password="",
            autocommit=True,
        )
        cur = admin.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        cur.close()
        admin.close()


def _build(creds: dict[str, str]) -> MysqlAdapter:
    adapter = MysqlAdapter(creds)
    adapter.connect()

    return adapter


class TestPhysicalLayout:
    """The declared partitioning key via `information_schema.partitions`."""

    def _cursor(self, mysql_test_db: dict[str, str]) -> Any:
        import mysql.connector

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        return conn, conn.cursor()

    def test_a_single_column_key_is_recovered(self, mysql_test_db: dict[str, str]) -> None:
        conn, cur = self._cursor(mysql_test_db)
        try:
            cur.execute(
                "CREATE TABLE zz_range (id INT, logged_at DATE) "
                "PARTITION BY RANGE (id) ("
                "PARTITION p0 VALUES LESS THAN (100), "
                "PARTITION p1 VALUES LESS THAN MAXVALUE)",
            )
        finally:
            cur.close()
            conn.close()

        adapter = _build(mysql_test_db)

        try:
            layout = adapter.introspect_physical_layout(f"{mysql_test_db['database']}.zz_range")
            assert layout is not None
            assert layout.mechanism == "partition"
            assert [k.expression for k in layout.keys] == ["`id`"]
            assert [k.column for k in layout.keys] == ["id"]
        finally:
            adapter.close()

    def test_an_expression_key_carries_no_base_column(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        """A function-call key names no single column a predicate matches on."""

        conn, cur = self._cursor(mysql_test_db)
        try:
            cur.execute(
                "CREATE TABLE zz_expr (id INT, logged_at DATE) "
                "PARTITION BY RANGE (YEAR(logged_at)) ("
                "PARTITION p0 VALUES LESS THAN (2024), "
                "PARTITION p1 VALUES LESS THAN MAXVALUE)",
            )
        finally:
            cur.close()
            conn.close()

        adapter = _build(mysql_test_db)

        try:
            layout = adapter.introspect_physical_layout(f"{mysql_test_db['database']}.zz_expr")
            assert layout is not None
            key = layout.keys[0]
            assert key.column is None
            assert "year" in key.expression.lower()
        finally:
            adapter.close()

    def test_a_multi_column_key_preserves_declaration_order(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        conn, cur = self._cursor(mysql_test_db)
        try:
            cur.execute(
                "CREATE TABLE zz_cols (country_code VARCHAR(8), logged_at DATE) "
                "PARTITION BY RANGE COLUMNS(country_code, logged_at) ("
                "PARTITION p0 VALUES LESS THAN ('m', '2024-01-01'), "
                "PARTITION p1 VALUES LESS THAN (MAXVALUE, MAXVALUE))",
            )
        finally:
            cur.close()
            conn.close()

        adapter = _build(mysql_test_db)

        try:
            layout = adapter.introspect_physical_layout(f"{mysql_test_db['database']}.zz_cols")
            assert layout is not None
            assert [k.column for k in layout.keys] == ["country_code", "logged_at"]
        finally:
            adapter.close()

    def test_an_unpartitioned_table_reports_absence(self, mysql_test_db: dict[str, str]) -> None:
        conn, cur = self._cursor(mysql_test_db)
        try:
            cur.execute("CREATE TABLE zz_plain (id INT)")
        finally:
            cur.close()
            conn.close()

        adapter = _build(mysql_test_db)

        try:
            fqn = f"{mysql_test_db['database']}.zz_plain"
            assert adapter.introspect_physical_layout(fqn) is None
        finally:
            adapter.close()


class TestRankedShapeSurvivesUserColumnNames:
    """The ranked derived table's own columns must not collide with the data's.

    A window's `ORDER BY` resolves against the select list before the table, so a column named
    like the rank or the total binds to the query's own aggregate and the statement fails,
    taking the whole table's profile with it.
    """

    @pytest.mark.parametrize("column", ["n", "rn", "score"])
    def test_a_column_named_like_the_ranking_helpers_still_profiles(
        self,
        mysql_test_db: dict[str, str],
        column: str,
    ) -> None:
        import mysql.connector

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        try:
            cursor = conn.cursor(buffered=True)
            cursor.execute(f"CREATE TABLE ranked_t (`{column}` INT)")
            # An int classifies numeric regardless of uniqueness (SPEC 4.2); repeats feed ranking.
            values = ",".join(f"({i % 60})" for i in range(200))
            cursor.execute(f"INSERT INTO ranked_t (`{column}`) VALUES {values}")
            cursor.close()
        finally:
            conn.close()

        adapter = _build(mysql_test_db)

        try:
            fqn = f"{mysql_test_db['database']}.ranked_t"
            cols = adapter.introspect_columns(fqn)
            _, stats = adapter.compute_statistics(
                fqn,
                cols,
                StatisticsConfig(enumeration_threshold=5),
                frozenset(),
            )
        finally:
            adapter.close()

        assert stats[column].percentiles, "the numeric branch produced no percentiles"
        assert stats[column].range is not None


class TestScopedSampling:
    """`looks_like` reads the rows the artifact covers, like every other field.

    `score < 10` leaves the labels numbered under ten, so any other value escaped the scope.
    The two sizes straddle `n * SMALL_TABLE_FACTOR` on the 200-row fixture, so one case takes
    the direct read and the other the over-sample.
    """

    @staticmethod
    def _sample(creds: dict[str, str], n: int, scope: TableScope | None) -> list:
        adapter = _build(creds)

        try:
            return adapter.sample_values(f"{creds['database']}.viability_check", "label", n, scope)
        finally:
            adapter.close()

    @staticmethod
    def _label_numbers(values: list) -> list[int]:
        return [int(str(v).removeprefix("label-")) for v in values]

    @pytest.mark.parametrize("n", [50, 5], ids=["direct", "over-sampled"])
    def test_a_filter_binds_to_the_sampled_values(
        self,
        mysql_test_db: dict[str, str],
        n: int,
    ) -> None:
        values = self._sample(mysql_test_db, n, TableScope(filter="score < 10"))

        assert values
        assert all(number < 10 for number in self._label_numbers(values))

    def test_the_same_call_unscoped_reaches_the_rest(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        """Negative control: without the scope the assertion above would not hold."""

        values = self._sample(mysql_test_db, 50, None)

        assert any(number >= 10 for number in self._label_numbers(values))


class TestAgainstMariaDB:
    def test_enum_column_classified_categorical(self, herbarium_sheet_db: dict[str, str]) -> None:
        adapter = _build(herbarium_sheet_db)
        fqn = f"{herbarium_sheet_db['database']}.herbarium_sheet"
        cols = adapter.introspect_columns(fqn)
        status = next(c for c in cols if c.name == "status")
        assert status.sql_type.startswith("enum")

        _, stats = adapter.compute_statistics(fqn, cols, StatisticsConfig(), frozenset())
        # Low-cardinality enum routes through the categorical value-list shape.
        assert stats["status"].values is not None
        adapter.close()

    def test_auto_increment_counter_stripped_from_ddl(
        self,
        herbarium_sheet_db: dict[str, str],
    ) -> None:
        adapter = _build(herbarium_sheet_db)
        ddl = adapter.extract_ddl(f"{herbarium_sheet_db['database']}.herbarium_sheet")
        assert "AUTO_INCREMENT=" not in ddl  # volatile table-option counter removed
        assert "AUTO_INCREMENT" in ddl  # column-level keyword preserved
        assert ddl.endswith("\n")
        adapter.close()

    def test_secondary_index_listed(self, herbarium_sheet_db: dict[str, str]) -> None:
        adapter = _build(herbarium_sheet_db)
        indexes = adapter.introspect_indexes(f"{herbarium_sheet_db['database']}.herbarium_sheet")
        names = {idx.name for idx in indexes}
        assert "herbarium_sheet_label_idx" in names
        adapter.close()

    def test_an_indexed_column_without_a_constraint_is_not_a_key(
        self,
        herbarium_sheet_db: dict[str, str],
    ) -> None:
        """`label` carries a non-unique KEY and no constraint, so only the PK is declared.

        MySQL backs both constraints and plain indexes with an index, so `non_unique` is the
        only discriminator.
        """

        adapter = _build(herbarium_sheet_db)
        keys = adapter.introspect_unique_keys(f"{herbarium_sheet_db['database']}.herbarium_sheet")

        assert keys == [UniqueKeyMeta(columns=("id",), primary=True)]
        adapter.close()

    def test_table_and_column_comments(self, herbarium_sheet_db: dict[str, str]) -> None:
        adapter = _build(herbarium_sheet_db)
        comments = adapter.extract_comments(f"{herbarium_sheet_db['database']}.herbarium_sheet")
        assert comments.table == "Herbarium sheet catalog"
        assert comments.columns.get("label") == "human-facing specimen label"
        adapter.close()


@pytest.fixture
def events_db(mysql_cluster: MysqlCluster) -> Iterator[dict[str, str]]:
    """Fresh database with a single mid-cardinality DATETIME column (60 distinct / 200 rows)."""

    from datetime import datetime, timedelta

    import mysql.connector

    db_name = "events_test"
    admin = mysql.connector.connect(
        host="127.0.0.1",
        port=mysql_cluster.port,
        user="root",
        password="",
        autocommit=True,
    )
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    cur.execute(f"CREATE DATABASE `{db_name}`")
    cur.close()
    admin.close()

    seeded = mysql.connector.connect(
        host="127.0.0.1",
        port=mysql_cluster.port,
        user="root",
        password="",
        database=db_name,
        autocommit=True,
    )
    cur = seeded.cursor()
    cur.execute(
        "CREATE TABLE curation_event (id INT PRIMARY KEY AUTO_INCREMENT, occurred_at DATETIME NOT NULL)",
    )
    base = datetime(2025, 1, 1, 12, 0, 0)  # noqa: DTZ001 - seeds a naive DATETIME column
    rows = [((base + timedelta(days=i % 60)).strftime("%Y-%m-%d %H:%M:%S"),) for i in range(200)]
    cur.executemany("INSERT INTO curation_event (occurred_at) VALUES (%s)", rows)
    cur.close()
    seeded.close()

    creds = {
        "host": "127.0.0.1",
        "port": str(mysql_cluster.port),
        "database": db_name,
        "user": "root",
        "password": "",
    }

    try:
        yield creds
    finally:
        admin = mysql.connector.connect(
            host="127.0.0.1",
            port=mysql_cluster.port,
            user="root",
            password="",
            autocommit=True,
        )
        cur = admin.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        cur.close()
        admin.close()


class TestDatetimeTemporalConformance:
    """Mid-cardinality MySQL datetime classifies temporal and prints conformance-clean."""

    _CONN = "mysql_dt"

    def _write_project(self, project_dir: Path) -> None:
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / ".dbprint.yaml").write_text(
            f"""\
defaults:
  max_age_days: 7
  statistics:
    enumeration_threshold: 50
    top_n_values: 20
    percentiles: [1, 25, 50, 75, 99]

connections:
  {self._CONN}:
    adapter: mysql
    auto: true
    output: prints
""",
        )

    def test_datetime_column_temporal_and_conformant(
        self,
        events_db: dict[str, str],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        project_dir = tmp_path / "project"
        self._write_project(project_dir)
        upper = self._CONN.upper()
        env = {
            f"DBPRINT_{upper}_HOST": events_db["host"],
            f"DBPRINT_{upper}_PORT": events_db["port"],
            f"DBPRINT_{upper}_DATABASE": events_db["database"],
            f"DBPRINT_{upper}_USER": events_db["user"],
            f"DBPRINT_{upper}_PASSWORD": events_db["password"],
        }
        monkeypatch.chdir(project_dir)

        for k, v in env.items():
            monkeypatch.setenv(k, v)

        result = CliRunner().invoke(main, ["generate", "--no-tui"])
        assert result.exit_code in (0, 3), result.output

        print_dir = project_dir / "prints" / self._CONN
        errors = [i for i in validate_print(print_dir) if i.severity == "error"]
        assert errors == [], "Conformance violations:\n" + "\n".join(
            f"  {e.code} at {e.path}: {e.detail}" for e in errors
        )

        stats = yaml.safe_load(
            (print_dir / events_db["database"] / "curation_event/statistics.yaml").read_text(),
        )
        occurred = stats["columns"]["occurred_at"]
        assert occurred["classification"] == "temporal"
        assert "range" in occurred
        assert "freshness" in occurred
        assert "percentiles" in occurred


def _split_sql(text: str) -> list[str]:
    """Split a fixture SQL script into individual statements (comments dropped)."""

    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("--")]

    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


@pytest.fixture
def years_db(mysql_cluster: MysqlCluster) -> Iterator[dict[str, str]]:
    """Fresh database with a mid-cardinality YEAR column (60 distinct / 200 rows)."""

    import mysql.connector

    db_name = "years_test"
    admin = mysql.connector.connect(
        host="127.0.0.1",
        port=mysql_cluster.port,
        user="root",
        password="",
        autocommit=True,
    )
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    cur.execute(f"CREATE DATABASE `{db_name}`")
    cur.close()
    admin.close()

    seeded = mysql.connector.connect(
        host="127.0.0.1",
        port=mysql_cluster.port,
        user="root",
        password="",
        database=db_name,
        autocommit=True,
    )
    cur = seeded.cursor(buffered=True)
    cur.execute(
        "CREATE TABLE intake_record (id INT PRIMARY KEY AUTO_INCREMENT, collected YEAR NOT NULL)",
    )
    cur.executemany(
        "INSERT INTO intake_record (collected) VALUES (%s)",
        [(YEAR_MIN + (i % 60),) for i in range(200)],
    )
    cur.execute("ANALYZE TABLE intake_record")
    cur.close()
    seeded.close()

    yield {
        "host": "127.0.0.1",
        "port": str(mysql_cluster.port),
        "database": db_name,
        "user": "root",
        "password": "",
    }

    admin = mysql.connector.connect(
        host="127.0.0.1",
        port=mysql_cluster.port,
        user="root",
        password="",
        autocommit=True,
    )
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    cur.close()
    admin.close()


YEAR_MIN = 1960
YEAR_MAX = YEAR_MIN + 59


class TestYearColumn:
    """MySQL `YEAR` is a temporal type that no date function will accept.

    `CAST(y AS DATE)` and `TIMESTAMPDIFF` return NULL for a YEAR operand silently, which a
    guard turning NULL into `0` would misreport as a 0-day span; `MAKEDATE(y, 1)` is the
    conversion MySQL does define.
    """

    def _stats(self, creds: dict[str, str]):
        adapter = MysqlAdapter(creds)
        adapter.connect()
        fqn = f"{creds['database']}.intake_record"
        columns = adapter.introspect_columns(fqn)
        _, stats = adapter.compute_statistics(fqn, columns, StatisticsConfig(), frozenset())

        return stats["collected"]

    def test_year_is_classified_temporal(self, years_db: dict[str, str]) -> None:
        collected = self._stats(years_db)
        assert collected.sql_type.startswith("year")
        assert collected.range is not None, "a temporal column must carry range"

    def test_span_covers_the_real_range_of_years(self, years_db: dict[str, str]) -> None:
        collected = self._stats(years_db)
        expected = (date(YEAR_MAX, 1, 1) - date(YEAR_MIN, 1, 1)).days

        assert collected.range is not None
        assert collected.range.span_days == expected

    def test_range_reports_the_years_themselves(self, years_db: dict[str, str]) -> None:
        """Only the arithmetic goes through a date; the values stay the years held."""

        collected = self._stats(years_db)
        assert collected.range is not None
        assert (collected.range.min, collected.range.max) == (YEAR_MIN, YEAR_MAX)

    def test_year_column_satisfies_the_statistics_schema(self, years_db: dict[str, str]) -> None:
        """The fields SPEC 2.2.3 marks R for temporal, within their schema bounds."""

        collected = self._stats(years_db)

        assert collected.range is not None
        assert collected.percentiles
        assert collected.distribution is not None
        assert collected.range.span_days is not None and collected.range.span_days >= 0


@pytest.fixture
def times_db(mysql_cluster: MysqlCluster) -> Iterator[dict[str, str]]:
    """Fresh database with a mid-cardinality TIME column (60 distinct / 200 rows)."""

    import mysql.connector

    db_name = "times_test"
    admin = mysql.connector.connect(
        host="127.0.0.1",
        port=mysql_cluster.port,
        user="root",
        password="",
        autocommit=True,
    )
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    cur.execute(f"CREATE DATABASE `{db_name}`")
    cur.close()
    admin.close()

    seeded = mysql.connector.connect(
        host="127.0.0.1",
        port=mysql_cluster.port,
        user="root",
        password="",
        database=db_name,
        autocommit=True,
    )
    cur = seeded.cursor(buffered=True)
    cur.execute(
        "CREATE TABLE field_round (id INT PRIMARY KEY AUTO_INCREMENT, run_at TIME NOT NULL)",
    )
    cur.executemany(
        "INSERT INTO field_round (run_at) VALUES (%s)",
        [(f"08:{i % 60:02d}:00",) for i in range(200)],
    )
    cur.close()
    seeded.close()

    yield {
        "host": "127.0.0.1",
        "port": str(mysql_cluster.port),
        "database": db_name,
        "user": "root",
        "password": "",
    }

    admin = mysql.connector.connect(
        host="127.0.0.1",
        port=mysql_cluster.port,
        user="root",
        password="",
        autocommit=True,
    )
    cur = admin.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
    cur.close()
    admin.close()


class TestTimeColumn:
    """MySQL returns TIME as a `timedelta` - an offset, not an instant.

    Date arithmetic against NOW() is undefined for it and the YAML core schema has no tag for
    the type, so it is rendered as the clock reading it stands for.
    """

    def _stats(self, creds: dict[str, str]):
        adapter = MysqlAdapter(creds)
        adapter.connect()
        fqn = f"{creds['database']}.field_round"
        columns = adapter.introspect_columns(fqn)
        _, stats = adapter.compute_statistics(fqn, columns, StatisticsConfig(), frozenset())

        return stats["run_at"]

    def test_the_table_extracts(self, times_db: dict[str, str]) -> None:
        """A `timedelta` reaching the dumper would raise before this returns.

        `times_db` cycles `run_at` over `i % 60` across 200 rows (see the fixture above), so
        60 is the distinct count independent of anything this module computes.
        """

        assert self._stats(times_db).cardinality == 60

    def test_span_is_zero_inside_one_day(self, times_db: dict[str, str]) -> None:
        run_at = self._stats(times_db)

        assert run_at.range is not None
        assert run_at.range.span_days == 0

    def test_range_renders_the_clock_reading(self, times_db: dict[str, str]) -> None:
        run_at = self._stats(times_db)

        assert run_at.range is not None
        assert (run_at.range.min, run_at.range.max) == ("08:00:00", "08:59:00")

    def test_every_emitted_value_is_yaml_representable(self, times_db: dict[str, str]) -> None:
        """SPEC 2.2.4 admits strings, numbers and booleans - not driver types."""

        from dbprint.engine.yaml_dumper import dump_yaml

        run_at = self._stats(times_db)

        assert run_at.percentiles
        assert all(isinstance(v, str) for v in run_at.percentiles.values())
        assert dump_yaml({"range": {"min": run_at.range.min}, "p": run_at.percentiles})


class _RecordingCursor:
    """Wraps a real MySQL cursor; records every statement text verbatim."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.statements: list[str] = []

    def execute(self, sql: object, params: object = None) -> _RecordingCursor:
        self.statements.append(str(sql))

        if params is None:
            self._real.execute(sql)
        else:
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

    def test_the_draw_is_sql_ordered_by_a_hash_of_the_value(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        """Asserts on the emitted statement text, which the behavioral checks cannot prove."""

        import mysql.connector

        from dbprint.adapters.mysql import looks_like as mysql_looks_like

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE sql_shape (v VARCHAR(64))")
            cursor.executemany(
                "INSERT INTO sql_shape (v) VALUES (%s)",
                [(f"val-{i}",) for i in range(100)],
            )
            cursor.close()

            recorder = _RecordingCursor(conn.cursor())
            fqn = f"{mysql_test_db['database']}.sql_shape"
            mysql_looks_like.sample_distinct(recorder, fqn, "v", n=50)
            flat = " ".join(" ".join(s.lower().split()) for s in recorder.statements)

            assert "order by" in flat and "md5(" in flat, (
                f"expected the distinct draw ordered by a hash of the value; "
                f"captured SQL: {recorder.statements}"
            )
        finally:
            conn.close()

    def test_a_value_inserted_last_is_still_reachable(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        """5,000 rows stays on the small path, where n=1000 clears `n * SMALL_TABLE_FACTOR`."""

        import mysql.connector

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE late_shape (v VARCHAR(64))")
            cursor.executemany(
                "INSERT INTO late_shape (v) VALUES (%s)",
                [(f"row-{i}",) for i in range(4000)],
            )
            cursor.executemany(
                "INSERT INTO late_shape (v) VALUES (%s)",
                [(f"11111111-1111-4111-8111-{i:012d}",) for i in range(1000)],
            )
            cursor.execute("ANALYZE TABLE late_shape")
            cursor.fetchall()  # ANALYZE TABLE returns a result set; consume before close()
            cursor.close()
        finally:
            conn.close()

        adapter = _build(mysql_test_db)

        try:
            fqn = f"{mysql_test_db['database']}.late_shape"
            values = adapter.sample_values(fqn, "v", n=1000)
            late = [v for v in values if str(v).startswith("11111111-1111-4111-8111-")]

            assert late, "the draw never reached a value inserted after the first n rows"
        finally:
            adapter.close()

    def test_two_draws_over_unchanged_data_agree(self, mysql_test_db: dict[str, str]) -> None:
        """The hash order is a function of the table's own seed, not session state.

        n=1000 keeps this on the small path: the large path's `ORDER BY RAND()` has no seed
        on MySQL (ARCHITECTURE.md's draw table), so only the small path repeats call to call.
        """

        import mysql.connector

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        try:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE stable_draw (v VARCHAR(64))")
            cursor.executemany(
                "INSERT INTO stable_draw (v) VALUES (%s)",
                [(f"val-{i}",) for i in range(5000)],
            )
            cursor.execute("ANALYZE TABLE stable_draw")
            cursor.fetchall()
            cursor.close()
        finally:
            conn.close()

        adapter = _build(mysql_test_db)

        try:
            fqn = f"{mysql_test_db['database']}.stable_draw"
            first = adapter.sample_values(fqn, "v", n=1000)
            second = adapter.sample_values(fqn, "v", n=1000)

            assert first == second
        finally:
            adapter.close()


class TestSuppressionReachesTextAlone:
    """MySQL's Phase B fallthrough serves two classifications, so its guard must not.

    Postgres and Snowflake reach the value-list branch only for `text`; MySQL builds both
    lists in one fallthrough, so `suppressed` is qualified by the pre-classification there
    and nowhere else.
    """

    @staticmethod
    def _seed(creds: dict[str, str]) -> None:
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
            cursor.execute("CREATE TABLE suppressible (note VARCHAR(255), ref_id INT)")
            # 100 distinct notes over 200 rows clears the enumeration threshold, landing
            # `note` on `text`; the fk set below routes `ref_id` to `foreign_key_candidate`.
            values = ",".join(f"('note number {i % 100} of the set', {i % 5})" for i in range(200))
            cursor.execute(f"INSERT INTO suppressible (note, ref_id) VALUES {values}")
            cursor.close()
        finally:
            conn.close()

    def _profile(
        self,
        creds: dict[str, str],
        suppress: frozenset[str],
    ) -> dict[str, ColumnStats]:
        from dbprint.config import StatisticsConfig

        adapter = _build(creds)
        fqn = f"{creds['database']}.suppressible"

        try:
            columns = adapter.introspect_columns(fqn)
            counts, base = adapter.compute_base_statistics(fqn, columns, StatisticsConfig())

            return adapter.compute_column_statistics(
                fqn,
                columns,
                StatisticsConfig(),
                counts,
                base,
                frozenset({"ref_id"}),
                suppress_values=suppress,
            )
        finally:
            adapter.close()

    def test_the_two_columns_take_the_branches_this_test_needs(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        """The control: without it the assertions below could pass vacuously."""

        self._seed(mysql_test_db)
        stats = self._profile(mysql_test_db, frozenset())

        assert stats["note"].values is not None
        assert stats["ref_id"].values is not None
        assert stats["note"].cardinality == 100
        assert stats["ref_id"].cardinality == 5

    def test_a_suppressed_text_column_loses_its_list(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        self._seed(mysql_test_db)
        stats = self._profile(mysql_test_db, frozenset({"note"}))

        assert stats["note"].values is None
        assert stats["note"].values_coverage is None
        assert stats["note"].distribution is None

    def test_a_foreign_key_candidate_keeps_its_list_even_when_named(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        """Its matrix row requires the list unconditionally, so the guard refuses.

        One branch builds both lists here; a guard on `suppressed` alone would strip it.
        """

        self._seed(mysql_test_db)
        stats = self._profile(mysql_test_db, frozenset({"note", "ref_id"}))

        assert stats["ref_id"].values is not None
        assert stats["ref_id"].values_coverage is not None
        assert stats["ref_id"].distribution is not None
        # The text column in the same call still is suppressed.
        assert stats["note"].values is None


class TestOutOfRangeTemporal:
    """A zero date, or a session `time_zone` change, does not corrupt a bound.

    `_fetch_temporal_block` renders bounds to text in SQL, because mysql-connector-python turns
    the zero-date sentinel `0000-00-00` into `NULL` on fetch - an empty column in Python.
    """

    @staticmethod
    def _seed(creds: dict[str, str], values: list[str]) -> None:
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
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute("CREATE TABLE germination_reading (id INT, taken_at DATETIME)")
            # 60 distinct minutes clear the enumeration threshold, so the column is temporal.
            rows = ",".join(f"({i}, '2020-01-01 00:{i % 60:02d}:00')" for i in range(240))
            cursor.execute(f"INSERT INTO germination_reading (id, taken_at) VALUES {rows}")

            for offset, value in enumerate(values):
                cursor.execute(
                    "INSERT INTO germination_reading (id, taken_at) VALUES (%s, %s)",
                    (240 + offset, value),
                )

            cursor.close()
        finally:
            conn.close()

    @staticmethod
    def _profile(creds: dict[str, str]) -> dict[str, ColumnStats]:
        adapter = _build(creds)
        fqn = f"{creds['database']}.germination_reading"

        try:
            columns = adapter.introspect_columns(fqn)

            return adapter.compute_statistics(fqn, columns, StatisticsConfig(), frozenset())[1]
        finally:
            adapter.close()

    def test_zero_date_profiles_and_is_marked(self, mysql_test_db: dict[str, str]) -> None:
        self._seed(mysql_test_db, ["0000-00-00 00:00:00"])
        stats = self._profile(mysql_test_db)["taken_at"]

        assert stats.range is not None
        assert stats.range.min == "0000-00-00T00:00:00"
        assert stats.unrepresentable == ("min",)

    def test_ordinary_values_render_byte_identical(self, mysql_test_db: dict[str, str]) -> None:
        """Ordinary values are unaffected by the out-of-range handling above."""

        self._seed(mysql_test_db, [])
        stats = self._profile(mysql_test_db)["taken_at"]

        assert stats.range is not None
        assert stats.range.min == "2020-01-01T00:00:00"
        assert stats.range.max == "2020-01-01T00:59:00"
        assert stats.unrepresentable is None

    def test_timestamp_rendering_is_independent_of_session_time_zone(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:

        from typing import cast

        import mysql.connector

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        try:
            cursor = conn.cursor(buffered=True)
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute("CREATE TABLE stamped (id INT, seen_at TIMESTAMP NULL)")
            cursor.execute("INSERT INTO stamped (id, seen_at) VALUES (1, '2024-01-01 12:00:00')")

            col = _column("timestamp", name="seen_at")

            cursor.execute("SET time_zone = '+00:00'")
            utc_rng, *_ = stats_module._fetch_temporal_block(
                cast(stats_module.Cursor, cursor),
                "stamped",
                col,
                1,
                StatisticsConfig(),
            )

            cursor.execute("SET time_zone = '+05:00'")
            shifted_rng, *_ = stats_module._fetch_temporal_block(
                cast(stats_module.Cursor, cursor),
                "stamped",
                col,
                1,
                StatisticsConfig(),
            )

            cursor.close()
        finally:
            conn.close()

        assert shifted_rng.min == utc_rng.min
        assert shifted_rng.max == utc_rng.max

    def test_value_list_rendering_is_independent_of_session_time_zone(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        """A low-cardinality TIMESTAMP's `values` must agree with `range` on the frame."""

        from typing import cast

        import mysql.connector

        conn = mysql.connector.connect(
            host=mysql_test_db["host"],
            port=int(mysql_test_db["port"]),
            user=mysql_test_db["user"],
            password="",
            database=mysql_test_db["database"],
            autocommit=True,
        )

        try:
            cursor = conn.cursor(buffered=True)
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute("CREATE TABLE stamped2 (id INT, seen_at TIMESTAMP NULL)")
            cursor.execute(
                "INSERT INTO stamped2 (id, seen_at) VALUES (1, '2024-01-01 12:00:00')",
            )

            col = _column("timestamp", name="seen_at")

            cursor.execute("SET time_zone = '+00:00'")
            utc_values, *_ = stats_module._fetch_value_list(
                cast(stats_module.Cursor, cursor),
                "stamped2",
                col,
                1,
                StatisticsConfig(),
            )

            cursor.execute("SET time_zone = '+05:00'")
            shifted_values, *_ = stats_module._fetch_value_list(
                cast(stats_module.Cursor, cursor),
                "stamped2",
                col,
                1,
                StatisticsConfig(),
            )

            cursor.close()
        finally:
            conn.close()

        assert shifted_values == utc_values

    def test_degradation_net_drops_bounds_not_the_table(
        self,
        mysql_test_db: dict[str, str],
    ) -> None:
        from unittest.mock import patch

        self._seed(mysql_test_db, [])

        with patch(
            "dbprint.adapters.mysql.stats._fetch_calendar_temporal_block",
            side_effect=RuntimeError("simulated failure"),
        ):
            stats = self._profile(mysql_test_db)

        assert stats["taken_at"].range is None
        assert stats["taken_at"].percentiles is None
        assert stats["taken_at"].unrepresentable is None
        assert stats["taken_at"].cardinality is not None
        assert stats["id"].cardinality is not None
