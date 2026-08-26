"""An adapter's SQL is validated against its own engine's dialect.

An adapter exercised only against a foreign substrate can pass every test while emitting SQL
its own engine rejects. Two account-free guards run off one sweep: every statement reaching
the live cursor is checked against the syntax fragments its vendor accepts, and every
classification-specific statistics branch must emit SQL. Neither proves the target engine
accepts the statement; only running it against that engine does.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Any

import pytest

from dbprint.adapters import Adapter, StatisticsConfig
from dbprint.adapters.dialect import VENDOR_SUPPORT, Dialect, Vendor
from dbprint.adapters.mysql import DIALECT as MYSQL_DIALECT
from dbprint.adapters.mysql import stats as mysql_stats
from dbprint.adapters.postgres import DIALECT as POSTGRES_DIALECT
from dbprint.adapters.postgres import stats as postgres_stats
from dbprint.adapters.snowflake import DIALECT as SNOWFLAKE_DIALECT
from dbprint.adapters.snowflake import stats as snowflake_stats


DIALECTS: dict[str, Dialect] = {
    "postgres": POSTGRES_DIALECT,
    "mysql": MYSQL_DIALECT,
    "snowflake": SNOWFLAKE_DIALECT,
}

STATS_MODULES: dict[str, ModuleType] = {
    "postgres": postgres_stats,
    "mysql": mysql_stats,
    "snowflake": snowflake_stats,
}

# Statistics helpers that run for one pre-classification each, owning SQL no other branch emits.
BRANCH_HELPERS = (
    "_fetch_value_list",
    "_fetch_numeric_block",
    "_fetch_temporal_block",
    "_approximate_distribution_via_top_n",
)

# The clause each vendor uses to bound a sample, emitted only past `n * SMALL_TABLE_FACTOR`
# rows, so its presence proves the sampling path ran rather than the DISTINCT shortcut.
SAMPLE_CLAUSES: dict[str, str] = {
    "postgres": "tablesample bernoulli",
    "mysql": "order by rand()",
    "snowflake": "sample row (",
}

NARROW_TABLES = ["*.curator", "*.herbarium"]


def _foreign_fragments(statement: str, vendor: Vendor) -> list[str]:
    """Fragments in `statement` that `vendor`'s engine does not accept."""

    flat = " ".join(statement.lower().split())

    return sorted(
        fragment
        for fragment, accepted_by in VENDOR_SUPPORT.items()
        if fragment in flat and vendor not in accepted_by
    )


class Recorder:
    """Every statement one adapter emitted, in order."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.bound: list[tuple[str, Any]] = []

    def record(self, sql: str, params: Any) -> None:
        self.statements.append(sql)

        if params is not None:
            self.bound.append((sql, params))

    def flattened(self) -> list[str]:
        """Statements with whitespace collapsed and case folded."""

        return [" ".join(s.lower().split()) for s in self.statements]


@pytest.fixture
def sweep(
    sql_adapter_factory: tuple[str, Callable[[], Adapter]],
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Sweep]:
    """Drive one adapter over the whole contract fixture, capturing its SQL."""

    vendor, factory = sql_adapter_factory
    module = STATS_MODULES[vendor]
    counts = _install_branch_spies(monkeypatch, module)
    classifications = _install_classification_spy(monkeypatch, module)
    adapter = factory()

    try:
        yield Sweep(
            vendor=vendor,
            adapter=adapter,
            branch_counts=counts,
            classifications=classifications,
        )
    finally:
        adapter.close()


class Sweep:
    """One adapter plus the recorder and spies watching it."""

    def __init__(
        self,
        vendor: str,
        adapter: Adapter,
        branch_counts: Counter[str],
        classifications: set[str],
    ) -> None:
        self.vendor = vendor
        self.adapter = adapter
        self.branch_counts = branch_counts
        self.classifications = classifications
        self.recorder = _install_recorder(adapter)

    @property
    def dialect(self) -> Dialect:
        return DIALECTS[self.vendor]

    def run(self, include: list[str] | None = None) -> Recorder:
        """Call every SQL-emitting adapter method over the selected tables."""

        config = StatisticsConfig()

        for table in self.adapter.list_tables(include=include or ["*"], exclude=[]):
            self.adapter.extract_ddl(table.fqn)
            columns = self.adapter.introspect_columns(table.fqn)
            relationships = self.adapter.introspect_relationships(table.fqn)
            self.adapter.introspect_indexes(table.fqn)
            self.adapter.introspect_unique_keys(table.fqn)
            self.adapter.introspect_physical_layout(table.fqn)
            self.adapter.extract_comments(table.fqn)
            self.adapter.estimate_row_count(table.fqn)

            if not columns:
                continue

            # Production passes a non-empty fk_source_columns; with an empty set the
            # foreign_key_candidate branch is unreachable.
            fk_columns = frozenset(c for fk in relationships for c in fk.column)
            self.adapter.compute_statistics(table.fqn, columns, config, fk_columns)

            for column in columns:
                self.adapter.sample_values(table.fqn, column.name, n=5)

        return self.recorder


class TestEveryAdapterIsCovered:
    """A fourth adapter must not be silently uncovered by these three tables."""

    def test_every_registered_adapter_declares_a_dialect(self) -> None:
        from dbprint.cli.adapter_registry import ADAPTERS

        missing = set(ADAPTERS) - set(DIALECTS)

        assert not missing, f"these adapters declare no DIALECT for the sweep: {sorted(missing)}"

    def test_every_registered_adapter_is_swept(self) -> None:
        from dbprint.cli.adapter_registry import ADAPTERS
        from tests.adapters.conftest import SQL_PARAMS

        missing = set(ADAPTERS) - set(SQL_PARAMS)

        assert not missing, f"these adapters are never swept: {sorted(missing)}"


class TestDialectConformance:
    def test_no_statement_carries_foreign_vendor_syntax(self, sweep: Sweep) -> None:
        recorder = sweep.run()
        offenders = [
            (statement, _foreign_fragments(statement, sweep.dialect.vendor))
            for statement in recorder.statements
            if _foreign_fragments(statement, sweep.dialect.vendor)
        ]

        assert not offenders, (
            f"{sweep.vendor} emitted SQL its own engine does not accept: "
            + "; ".join(f"{fragments} in {' '.join(sql.split())!r}" for sql, fragments in offenders)
        )

    def test_bound_statements_use_the_declared_placeholder(self, sweep: Sweep) -> None:
        recorder = sweep.run()
        placeholder = sweep.dialect.placeholder
        missing = [sql for sql, _ in recorder.bound if placeholder not in sql]

        assert recorder.bound, f"{sweep.vendor} bound no parameters; the check would be vacuous."
        assert not missing, (
            f"{sweep.vendor} binds parameters without its declared {placeholder!r} placeholder: "
            + "; ".join(repr(" ".join(sql.split())) for sql in missing)
        )

    def test_no_statement_carries_another_paramstyles_placeholder(self, sweep: Sweep) -> None:
        recorder = sweep.run()
        foreign = {d.placeholder for d in DIALECTS.values()} - {sweep.dialect.placeholder}
        offenders = [sql for sql in recorder.statements if any(marker in sql for marker in foreign)]

        assert not offenders, (
            f"{sweep.vendor} emitted a placeholder its driver's paramstyle "
            f"({sweep.dialect.paramstyle}) does not bind: "
            + "; ".join(repr(" ".join(sql.split())) for sql in offenders)
        )


class TestBranchExecution:
    def test_every_statistics_branch_executes(self, sweep: Sweep) -> None:
        sweep.run()
        unexecuted = [name for name in BRANCH_HELPERS if not sweep.branch_counts[name]]

        assert not unexecuted, (
            f"{sweep.vendor}: these statistics branches emitted no SQL against the "
            f"contract fixture, so their dialect went unchecked: {unexecuted}"
        )

    def test_sampling_path_executes(self, sweep: Sweep) -> None:
        recorder = sweep.run()
        clause = SAMPLE_CLAUSES[sweep.vendor]

        assert any(clause in statement for statement in recorder.flattened()), (
            f"{sweep.vendor}: no statement carried {clause!r}, so every sample_values call "
            "took the small-table DISTINCT shortcut and the sampling SQL went unchecked."
        )

    def test_every_pre_classification_is_reached(self, sweep: Sweep) -> None:
        """Helper counters cannot separate the branches that share a helper.

        `foreign_key_candidate` and `text` both call `_fetch_value_list`, and the FK branch
        is the one a `frozenset()` caller never reaches.
        """

        sweep.run()
        expected = {"categorical", "foreign_key_candidate", "numeric", "temporal"}
        missing = expected - sweep.classifications

        assert not missing, (
            f"{sweep.vendor}: no column pre-classified as {sorted(missing)}, so those "
            f"branches emitted no SQL. Reached: {sorted(sweep.classifications)}"
        )


class TestNarrowTablesAloneAreNotEnough:
    """The wide table is what earns the branch coverage - prove it.

    Negative controls, so each first establishes the narrow sweep ran: an absence assertion
    over a selector that matched nothing would pass while proving nothing.
    """

    def test_narrow_tables_leave_branches_unexecuted(self, sweep: Sweep) -> None:
        recorder = sweep.run(include=NARROW_TABLES)

        assert recorder.statements, "narrow selectors matched no tables; the control is vacuous"
        assert sweep.branch_counts["_fetch_value_list"], (
            "the narrow tables should still reach the value-list branch"
        )

        reached = [name for name in BRANCH_HELPERS if sweep.branch_counts[name]]

        assert "_fetch_numeric_block" not in reached
        assert "_fetch_temporal_block" not in reached

    def test_narrow_tables_leave_the_sampling_path_unexecuted(self, sweep: Sweep) -> None:
        recorder = sweep.run(include=NARROW_TABLES)
        clause = SAMPLE_CLAUSES[sweep.vendor]

        assert recorder.statements, "narrow selectors matched no tables; the control is vacuous"
        assert not any(clause in statement for statement in recorder.flattened())


class TestGuardIsNotVacuous:
    @pytest.mark.parametrize(
        ("statement", "vendor", "expected"),
        [
            (
                "SELECT sql FROM duckdb_tables() WHERE schema_name = ?",
                "snowflake",
                "duckdb_tables(",
            ),
            ("SELECT COUNT(*) FILTER (WHERE c IS NULL) FROM t", "snowflake", "filter (where"),
            ("SELECT EXTRACT(EPOCH FROM (now() - MAX(c))) FROM t", "snowflake", "extract(epoch"),
            ("SELECT * FROM t USING SAMPLE 50 ROWS", "snowflake", "using sample"),
            ("SELECT COUNT_IF(c IS NULL) FROM t", "postgres", "count_if("),
            ("SELECT * FROM `db`.`t`", "postgres", "`"),
            ("SELECT n_distinct FROM pg_stats WHERE tablename = %s", "mysql", "pg_stats"),
            ("SELECT * FROM t SAMPLE ROW (50 ROWS)", "mysql", "sample row ("),
        ],
    )
    def test_foreign_fragment_is_reported(
        self,
        statement: str,
        vendor: Vendor,
        expected: str,
    ) -> None:
        assert expected in _foreign_fragments(statement, vendor)

    @pytest.mark.parametrize(
        ("statement", "vendor"),
        [
            ("SELECT GET_DDL('TABLE', 'db.s.t')", "snowflake"),
            ('SHOW IMPORTED KEYS IN TABLE "DB"."S"."T"', "snowflake"),
            ("SELECT COUNT(*) FILTER (WHERE c IS NULL) FROM t", "postgres"),
            ("SELECT c.reltuples::double precision FROM pg_class c", "postgres"),
            ("SELECT * FROM `db`.`t` ORDER BY RAND() LIMIT %s", "mysql"),
            ("SHOW CREATE TABLE `db`.`t`", "mysql"),
        ],
    )
    def test_native_statement_is_clean(self, statement: str, vendor: Vendor) -> None:
        assert _foreign_fragments(statement, vendor) == []


class TestDeclaredParamstyleMatchesDriver:
    """The declared placeholder is only correct if the driver is opened for it."""

    def test_snowflake_connector_is_opened_with_qmark(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from dbprint.adapters.snowflake import connection as snowflake_connection

        captured = _capture_connect_kwargs(monkeypatch, snowflake_connection, "snowflake.connector")
        snowflake_connection._default_cursor_factory(
            snowflake_connection.ConnectionParams(
                account="a",
                user="u",
                warehouse="w",
                database="d",
                role="r",
                password="p",
            ),
        )

        assert captured["paramstyle"] == SNOWFLAKE_DIALECT.paramstyle

    @pytest.mark.parametrize(
        ("module_path", "driver", "driver_default", "dialect"),
        [
            ("dbprint.adapters.postgres.connection", "psycopg", "pyformat", POSTGRES_DIALECT),
            ("dbprint.adapters.mysql.connection", "mysql.connector", "pyformat", MYSQL_DIALECT),
        ],
    )
    def test_driver_default_is_kept_and_matches_the_declaration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module_path: str,
        driver: str,
        driver_default: str,
        dialect: Dialect,
    ) -> None:
        """These drivers are left on their own default, so it must be the declared one.

        `driver_default` is the driver's documented paramstyle, held here as independent
        ground truth: the adapter must pass no override, and the declaration must agree.
        """

        import importlib

        module = importlib.import_module(module_path)
        captured = _capture_connect_kwargs(monkeypatch, module, driver)
        monkeypatch.setattr(module, "ensure_pg_dump_available", lambda: None, raising=False)
        connection = module.Connection(
            module.ConnectionParams(host="h", port=1, database="d", user="u", password="p"),
        )
        connection.open()

        assert captured, f"{module_path} never reached the driver; the check would be vacuous."
        assert "paramstyle" not in captured, (
            f"{module_path} overrides paramstyle; the declared placeholder is derived "
            "from the driver default and would no longer describe what it binds."
        )
        assert dialect.paramstyle == driver_default


def _install_recorder(adapter: Any) -> Recorder:
    """Wrap the adapter's live cursor so every emitted statement is captured.

    Reaches into the connection object because the cursor is absent from the Adapter surface.
    """

    recorder = Recorder()
    connection = adapter._connection

    for attribute in ("_conn", "_cursor"):
        target = getattr(connection, attribute, None)

        if target is not None:
            setattr(connection, attribute, _RecordingProxy(target, recorder))

    return recorder


class _RecordingProxy:
    """Forward everything to the wrapped cursor/connection; record `execute`."""

    def __init__(self, target: Any, recorder: Recorder) -> None:
        self._target = target
        self._recorder = recorder

    def execute(self, sql: str, params: Any = None) -> Any:
        self._recorder.record(sql, params)

        if params is None:
            return self._target.execute(sql)

        return self._target.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._target, name)


def _install_branch_spies(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> Counter[str]:
    """Count calls to each classification-specific statistics helper."""

    counts: Counter[str] = Counter()

    for name in BRANCH_HELPERS:
        monkeypatch.setattr(module, name, _counting(name, getattr(module, name), counts))

    return counts


def _counting(name: str, original: Any, counts: Counter[str]) -> Any:
    def spy(*args: Any, **kwargs: Any) -> Any:
        counts[name] += 1

        return original(*args, **kwargs)

    return spy


def _install_classification_spy(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> set[str]:
    """Collect every pre-classification the adapter's dispatch actually assigned."""

    seen: set[str] = set()
    original = module._pre_classify

    def spy(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        seen.add(result)

        return result

    monkeypatch.setattr(module, "_pre_classify", spy)

    return seen


def _capture_connect_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    driver_name: str,
) -> dict[str, Any]:
    """Swap the lazily-imported driver for a stub that records `connect` kwargs."""

    captured: dict[str, Any] = {}

    class _StubError(Exception):
        pass

    class _StubCursor:
        def close(self) -> None: ...

    class _StubConnection:
        def cursor(self, **kwargs: Any) -> _StubCursor:
            return _StubCursor()

        def close(self) -> None: ...

        def is_connected(self) -> bool:
            return True

    class _StubDriver:
        Error = _StubError

        @staticmethod
        def connect(**kwargs: Any) -> _StubConnection:
            captured.update(kwargs)

            return _StubConnection()

    def fake_import(name: str) -> Any:
        assert name == driver_name, f"unexpected lazy import of {name!r}"

        return _StubDriver

    monkeypatch.setattr(module.importlib, "import_module", fake_import)

    return captured
