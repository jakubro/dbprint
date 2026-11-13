"""SqliteAdapter-specific tests - behavior the generic contract suite does not cover."""

from __future__ import annotations

import hashlib
import signal
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from dbprint.adapters import SqliteAdapter, StatisticsConfig, TableScope
from dbprint.adapters.errors import NoResultSet, QueryFailed
from dbprint.adapters.interrupt import Interrupted
from dbprint.adapters.sqlite import SqliteConnectionError


_SERIES = (
    "WITH RECURSIVE gen(value) AS (SELECT 1 UNION ALL SELECT gen.value + 1 FROM gen "
    "WHERE gen.value < 400)"
)


def _memory(*statements: str) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:", check_same_thread=False, autocommit=True)

    for statement in statements:
        connection.execute(statement)

    return connection


@pytest.fixture
def open_adapter() -> Iterator[Callable[..., SqliteAdapter]]:
    opened: list[SqliteAdapter] = []

    def build(*statements: str) -> SqliteAdapter:
        connection = _memory(*statements)
        adapter = SqliteAdapter(
            {"database": ":memory:"},
            cursor_factory=lambda _p: connection.cursor(),
        )
        adapter.connect()
        adapter.list_tables(include=["*"], exclude=[])
        opened.append(adapter)

        return adapter

    try:
        yield build
    finally:
        for adapter in opened:
            adapter.close()


def _file(path: Path, *statements: str) -> Path:
    connection = sqlite3.connect(path)

    for statement in statements:
        connection.execute(statement)

    connection.commit()
    connection.close()

    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestTheFileIsOnlyRead:
    def test_a_profile_and_a_sampled_copy_leave_the_file_byte_identical(
        self,
        tmp_path: Path,
    ) -> None:
        path = _file(
            tmp_path / "garden.sqlite",
            "CREATE TABLE plot (id INTEGER PRIMARY KEY, label TEXT COLLATE NOCASE)",
            f"{_SERIES} INSERT INTO plot (label) SELECT 'p' || gen.value FROM gen",
        )
        before = _digest(path)
        adapter = SqliteAdapter({"database": str(path)})
        adapter.connect()

        try:
            [table] = adapter.list_tables(include=["*"], exclude=[])
            columns = adapter.introspect_columns(table.fqn)
            scope = adapter.materialize_scope(table.fqn, TableScope(sample=0.5))
            adapter.compute_statistics(
                table.fqn,
                columns,
                StatisticsConfig(),
                frozenset(),
                scope=scope,
            )
            adapter.release_scope(table.fqn, scope)
        finally:
            adapter.close()

        assert _digest(path) == before
        assert sorted(p.name for p in tmp_path.iterdir()) == ["garden.sqlite"]

    def test_a_file_in_wal_mode_is_read_without_writing_it(self, tmp_path: Path) -> None:
        path = _file(
            tmp_path / "garden.sqlite",
            "PRAGMA journal_mode = WAL",
            "CREATE TABLE plot (id INTEGER PRIMARY KEY, label TEXT)",
            "INSERT INTO plot (label) VALUES ('a'), ('b')",
        )
        before = _digest(path)
        adapter = SqliteAdapter({"database": str(path)})
        adapter.connect()

        try:
            [table] = adapter.list_tables(include=["*"], exclude=[])
            columns = adapter.introspect_columns(table.fqn)
            counts, _ = adapter.compute_statistics(
                table.fqn,
                columns,
                StatisticsConfig(),
                frozenset(),
            )
        finally:
            adapter.close()

        assert counts.rows_scanned == 2
        assert _digest(path) == before

    def test_a_write_is_refused(self, tmp_path: Path) -> None:
        path = _file(tmp_path / "garden.sqlite", "CREATE TABLE plot (id INTEGER)")
        adapter = SqliteAdapter({"database": str(path)})
        adapter.connect()

        try:
            with pytest.raises(QueryFailed, match="readonly"):
                adapter.execute_query("INSERT INTO plot VALUES (1)")
        finally:
            adapter.close()

    def test_attaching_another_file_is_refused_and_creates_nothing(self, tmp_path: Path) -> None:
        path = _file(tmp_path / "garden.sqlite", "CREATE TABLE plot (id INTEGER)")
        adapter = SqliteAdapter({"database": str(path)})
        adapter.connect()

        try:
            with pytest.raises(QueryFailed, match="too many attached databases"):
                adapter.execute_query(f"ATTACH DATABASE '{tmp_path / 'other.sqlite'}' AS other")
        finally:
            adapter.close()

        assert not (tmp_path / "other.sqlite").exists()

    def test_a_missing_file_is_refused_and_not_created(self, tmp_path: Path) -> None:
        adapter = SqliteAdapter({"database": str(tmp_path / "absent.sqlite")})

        with pytest.raises(SqliteConnectionError, match="unable to open database file"):
            adapter.connect()

        assert not (tmp_path / "absent.sqlite").exists()

    def test_a_library_without_json_built_in_is_refused_before_the_file_opens(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = _file(tmp_path / "plot.sqlite", "CREATE TABLE plot (id INTEGER)")
        monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 37, 2))
        opened: list[object] = []
        adapter = SqliteAdapter({"database": str(path)}, cursor_factory=opened.append)

        with pytest.raises(SqliteConnectionError, match=r"older than 3\.38\.0"):
            adapter.connect()

        assert opened == []


class TestTheCatalog:
    def test_reserved_and_shadow_tables_are_left_out(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER PRIMARY KEY, label TEXT)",
            "CREATE INDEX plot_label_ix ON plot (label)",
            "INSERT INTO plot (label) VALUES ('a'), ('b')",
            "ANALYZE",
            "CREATE VIRTUAL TABLE field_notes USING fts5(body)",
            "CREATE VIEW plot_v AS SELECT id FROM plot",
        )

        listed = {t.fqn: t.type for t in adapter.list_tables(include=["*"], exclude=[])}

        assert listed == {"main.field_notes": "table", "main.plot": "table", "main.plot_v": "view"}

    def test_a_mixed_case_name_is_keyed_folded_and_read_as_spelled(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            'CREATE TABLE "Plot" ("LotCode" TEXT, cohort INTEGER)',
            "INSERT INTO \"Plot\" VALUES ('a', 1), ('b', 2)",
        )

        columns = adapter.introspect_columns("main.plot")

        assert [(c.name, c.physical_name) for c in columns] == [
            ("lotcode", "LotCode"),
            ("cohort", None),
        ]

    def test_ddl_is_the_stored_text_then_its_indexes_each_terminated(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER PRIMARY KEY, code TEXT UNIQUE, label TEXT)",
            "CREATE INDEX plot_label_ix ON plot (label)",
            "CREATE UNIQUE INDEX plot_code_label_ux ON plot (code, label)",
            "CREATE VIEW plot_v AS SELECT id FROM plot",
        )

        assert adapter.extract_ddl("main.plot") == (
            "CREATE TABLE plot (id INTEGER PRIMARY KEY, code TEXT UNIQUE, label TEXT);\n"
            "\n"
            "CREATE INDEX plot_label_ix ON plot (label);\n"
            "\n"
            "CREATE UNIQUE INDEX plot_code_label_ux ON plot (code, label);\n"
        )
        assert adapter.extract_ddl("main.plot_v") == "CREATE VIEW plot_v AS SELECT id FROM plot;\n"

    def test_a_statement_ending_in_a_line_comment_is_terminated_on_its_own_line(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER, code TEXT)",
            "CREATE INDEX plot_code_ix ON plot (code) -- by code",
            "CREATE VIEW plot_v AS SELECT id FROM plot -- the ids",
        )

        assert adapter.extract_ddl("main.plot") == (
            "CREATE TABLE plot (id INTEGER, code TEXT);\n"
            "\n"
            "CREATE INDEX plot_code_ix ON plot (code) -- by code\n"
            ";\n"
        )
        assert adapter.extract_ddl("main.plot_v") == (
            "CREATE VIEW plot_v AS SELECT id FROM plot -- the ids\n;\n"
        )

    def test_a_statement_ending_in_an_open_block_comment_ends_after_closing_it(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER)",
            "CREATE VIEW plot_v AS SELECT id FROM plot /* ids",
        )

        assert (
            adapter.extract_ddl("main.plot_v")
            == "CREATE VIEW plot_v AS SELECT id FROM plot /* ids */;\n"
        )

    def test_a_foreign_key_naming_no_column_targets_the_parent_primary_key(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE garden (site TEXT, lot INTEGER, PRIMARY KEY (site, lot))",
            "CREATE TABLE plot (id INTEGER, site TEXT, lot INTEGER, "
            "FOREIGN KEY (site, lot) REFERENCES garden ON DELETE CASCADE)",
        )

        [edge] = adapter.introspect_relationships("main.plot")

        assert (edge.column, edge.target_table, edge.target_column) == (
            ("site", "lot"),
            "main.garden",
            ("site", "lot"),
        )
        assert (edge.on_delete, edge.on_update, edge.constraint_name) == (
            "CASCADE",
            "NO ACTION",
            None,
        )

    @pytest.mark.parametrize(
        "parent",
        [
            "CREATE TABLE garden (site TEXT)",
            "CREATE TABLE garden (site TEXT PRIMARY KEY, lot INTEGER)",
            None,
        ],
    )
    def test_a_foreign_key_whose_parent_key_cannot_be_resolved_declares_no_edge(
        self,
        open_adapter: Callable[..., SqliteAdapter],
        parent: str | None,
    ) -> None:
        adapter = open_adapter(
            *([parent] if parent else []),
            "CREATE TABLE plot (id INTEGER, site TEXT, lot INTEGER, "
            "FOREIGN KEY (site, lot) REFERENCES garden)",
        )

        assert adapter.introspect_relationships("main.plot") == []

    def test_keys_and_indexes_keep_only_their_column_members(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER PRIMARY KEY, code TEXT UNIQUE, label TEXT, lot INTEGER)",
            "CREATE INDEX plot_label_ix ON plot (label)",
            "CREATE INDEX plot_expr_ix ON plot (lot + 1)",
            "CREATE UNIQUE INDEX plot_label_lot_ux ON plot (label, lot)",
            "CREATE UNIQUE INDEX plot_partial_ux ON plot (lot) WHERE lot > 0",
            "CREATE UNIQUE INDEX plot_lower_ux ON plot (lower(label))",
        )

        keys = [(k.columns, k.primary) for k in adapter.introspect_unique_keys("main.plot")]
        indexes = [(i.name, i.columns) for i in adapter.introspect_indexes("main.plot")]

        assert keys == [(("id",), True), (("code",), False), (("label", "lot"), False)]
        assert indexes == [("plot_label_ix", ("label",))]

    def test_a_column_collation_is_read_off_a_compound_select(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (a TEXT COLLATE NOCASE, b TEXT COLLATE RTRIM, c TEXT)",
        )

        assert [c.collation for c in adapter.introspect_columns("main.plot")] == [
            "nocase",
            "rtrim",
            None,
        ]

    def test_a_view_names_what_it_reads_one_step_deep(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER, lot INTEGER)",
            "CREATE TABLE garden (id INTEGER)",
            "CREATE VIEW plot_v AS SELECT p.id FROM plot p JOIN garden g ON g.id = p.lot",
            "CREATE VIEW plot_count_v AS SELECT COUNT(1) AS n FROM plot_v",
        )

        assert adapter.introspect_view_dependencies() == {
            "main.plot_count_v": ("main.plot_v",),
            "main.plot_v": ("main.garden", "main.plot"),
        }

    def test_a_view_names_each_object_it_reads_however_sqlite_flattens_it(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER, lot INTEGER)",
            "CREATE TABLE garden (id INTEGER)",
            "CREATE VIEW a_v AS SELECT 1 AS x FROM garden",
            "CREATE VIEW b_v AS SELECT p.id FROM plot p JOIN garden g ON 1",
            "CREATE VIEW plot_v AS SELECT * FROM plot",
            "CREATE VIEW plot_count_v AS SELECT count(*) AS n FROM plot_v",
            "CREATE VIEW c_v AS SELECT p.lot FROM plot p JOIN plot_v v ON v.id = p.id",
            "CREATE VIEW d_v AS SELECT 1 AS k FROM a_v",
            "CREATE VIEW e_v AS WITH plot AS (SELECT id FROM garden) SELECT id FROM plot",
        )

        assert adapter.introspect_view_dependencies() == {
            "main.c_v": ("main.plot", "main.plot_v"),
            "main.d_v": ("main.a_v",),
            "main.e_v": ("main.garden",),
            "main.b_v": ("main.garden", "main.plot"),
            "main.a_v": ("main.garden",),
            "main.plot_count_v": ("main.plot_v",),
            "main.plot_v": ("main.plot",),
        }

    def test_a_view_sqlite_cannot_prepare_costs_only_that_view(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER)",
            "CREATE TABLE garden (id INTEGER)",
            "CREATE VIEW plot_v AS SELECT id FROM plot",
            "CREATE VIEW garden_v AS SELECT id FROM garden",
            "DROP TABLE garden",
        )

        assert adapter.introspect_view_dependencies() == {"main.plot_v": ("main.plot",)}

    def test_a_view_has_no_row_count_and_a_table_an_exact_one(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER)",
            "INSERT INTO plot VALUES (1), (2), (3)",
            "CREATE VIEW plot_v AS SELECT id FROM plot",
        )

        assert (
            adapter.estimate_row_count("main.plot"),
            adapter.estimate_row_count("main.plot_v"),
        ) == (
            3,
            None,
        )


class TestADeclaredTypeHoldsOnlyWhileItsValuesFit:
    @pytest.mark.parametrize(
        ("declared", "values", "classify_as"),
        [
            ("INTEGER", "(5), ('n/a'), (300)", "text"),
            ("INTEGER", "(5), (300)", "integer"),
            ("DECIMAL(10,2)", "('500.0'), (12)", "integer"),
            ("DECIMAL(10,2)", "(500.5), (12)", "real"),
            ("DATE", "('2026-03-04'), ('2026-03-05 06:00')", None),
            ("DATE", "('2026-03-04'), ('unknown')", "text"),
            ("BOOLEAN", "(1), (0)", None),
            ("BOOLEAN", "(1), ('yes')", "text"),
            ("", "(1), (2.5)", "real"),
            ("", "(x'00'), (x'ff')", "blob"),
            ("nvarchar(8)", "('a'), ('b')", "text"),
            ("INT2", "(1), (2)", "integer"),
            ("JSON", "('{\"a\": 1}'), ('[1]')", None),
            ("JSON", "('{\"a\": 1}'), ('not json')", "text"),
            ("POINT", "(1), (2)", "integer"),
            ("TEXT[]", "('a'), ('b')", "text"),
            ("ARRAY", "(1), (2)", "integer"),
        ],
    )
    def test_a_read_column_classifies_by_name_affinity_and_values(
        self,
        open_adapter: Callable[..., SqliteAdapter],
        declared: str,
        values: str,
        classify_as: str | None,
    ) -> None:
        adapter = open_adapter(
            f"CREATE TABLE plot (c {declared})",
            f"INSERT INTO plot VALUES {values}",
        )

        [column] = adapter.read_columns("main.plot", adapter.introspect_columns("main.plot"))

        assert (column.sql_type, column.classify_as) == (declared, classify_as)

    @pytest.mark.parametrize("fqn", ["main.plot", "main.plot_v"])
    def test_the_catalog_classifies_by_the_declared_type_alone(
        self,
        open_adapter: Callable[..., SqliteAdapter],
        fqn: str,
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (label INTEGER, tag)",
            "INSERT INTO plot VALUES ('n/a', 1)",
            "CREATE VIEW plot_v AS SELECT label, tag FROM plot",
        )

        columns = adapter.introspect_columns(fqn)

        assert [(c.sql_type, c.classify_as) for c in columns] == [
            ("INTEGER", None),
            ("", "text") if fqn == "main.plot" else ("BLOB", None),
        ]

    def test_a_read_view_column_classifies_by_its_values(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (label INTEGER)",
            "INSERT INTO plot VALUES (5), ('n/a')",
            "CREATE VIEW plot_v AS SELECT label, label + 1 AS next FROM plot",
        )

        columns = adapter.read_columns("main.plot_v", adapter.introspect_columns("main.plot_v"))

        assert [c.classify_as for c in columns] == ["text", "integer"]


class TestTheProbesCompareAsTheColumnDoes:
    @pytest.mark.parametrize(
        ("collation", "spellings"),
        [("NOCASE", "('a', 1), ('A', 1), ('b', 2)"), ("RTRIM", "('a', 1), ('a ', 1), ('b', 2)")],
    )
    def test_a_pair_counts_its_spellings_as_one_value(
        self,
        open_adapter: Callable[..., SqliteAdapter],
        collation: str,
        spellings: str,
    ) -> None:
        adapter = open_adapter(
            f"CREATE TABLE plot (code TEXT COLLATE {collation}, lot INTEGER)",
            f"INSERT INTO plot VALUES {spellings}",
        )
        columns = adapter.read_columns("main.plot", adapter.introspect_columns("main.plot"))
        counts, phase_a = adapter.compute_base_statistics("main.plot", columns, StatisticsConfig())

        strengths = adapter.probe_dependencies(
            "main.plot",
            columns,
            counts,
            phase_a.stats,
            (("code", "lot"),),
        )

        assert strengths == {("code", "lot"): 1.0}


class TestStatisticsReadEachValueAsItsStorageClass:
    def test_a_temporal_column_publishes_the_stored_text_in_instant_order(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        stamps = [f"2018-11-27 06:43:{s:02d}.0" for s in range(60)] + ["2018-11-27T07:00:00"]
        rows = ", ".join(f"('{stamp}')" for stamp in stamps)
        adapter = open_adapter(
            "CREATE TABLE field_log (withdrawn_at DATETIME)",
            f"INSERT INTO field_log VALUES {rows}",
        )
        columns = adapter.introspect_columns("main.field_log")

        _, stats = adapter.compute_statistics(
            "main.field_log",
            columns,
            StatisticsConfig(),
            frozenset(),
        )

        seen = stats["withdrawn_at"]
        assert seen.range is not None
        assert (seen.range.min, seen.range.max, seen.range.span_days) == (
            "2018-11-27 06:43:00.0",
            "2018-11-27T07:00:00",
            0,
        )
        assert seen.quantized_count == 0

    def test_two_texts_naming_one_instant_rank_by_code_point_whatever_the_collation(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        days = [f"2018-{month:02d}-{day:02d}" for month in (3, 4) for day in range(1, 31)]
        stamps = [*days, "2018-01-01 ", "2018-01-01"]
        rows = ", ".join(f"('{stamp}')" for stamp in stamps * 2)
        adapter = open_adapter(
            "CREATE TABLE field_log (withdrawn_at DATE COLLATE RTRIM)",
            f"INSERT INTO field_log VALUES {rows}",
        )
        columns = adapter.read_columns(
            "main.field_log",
            adapter.introspect_columns("main.field_log"),
        )

        _, stats = adapter.compute_statistics(
            "main.field_log",
            columns,
            StatisticsConfig(),
            frozenset(),
        )

        seen = stats["withdrawn_at"]
        assert seen.range is not None
        assert seen.range.min == "2018-01-01"

    def test_midnight_counts_in_every_spelling_julianday_reads(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        midnights = ("{} 00:00:00 +02:00", "{}T00:00:00z", "{} 00:00", "{} 00:00:00.000Z")
        days = [f"2018-{month:02d}-{day:02d}" for month in (3, 4) for day in range(1, 31)]
        stamps = [midnights[i % 4].format(day) for i, day in enumerate(days[:40])]
        stamps += [f"{day} 06:00:00" for day in days[40:]]
        rows = ", ".join(f"('{stamp}')" for stamp in stamps)
        adapter = open_adapter(
            "CREATE TABLE field_log (withdrawn_at DATETIME)",
            f"INSERT INTO field_log VALUES {rows}",
        )
        columns = adapter.read_columns(
            "main.field_log",
            adapter.introspect_columns("main.field_log"),
        )

        _, stats = adapter.compute_statistics(
            "main.field_log",
            columns,
            StatisticsConfig(),
            frozenset(),
        )

        assert stats["withdrawn_at"].quantized_count == 40

    def test_a_list_classified_integer_column_holding_text_measures_no_length(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (label INTEGER, tier INT2)",
            "INSERT INTO plot VALUES (5, 1), ('n/a', 2), (300, 3)",
        )
        columns = adapter.introspect_columns("main.plot")

        _, stats = adapter.compute_statistics("main.plot", columns, StatisticsConfig(), frozenset())

        assert [v.value for v in stats["label"].values or ()] == [300, 5, "n/a"]
        assert stats["label"].length is None
        assert stats["tier"].length is not None

    def test_a_sampled_draw_is_the_same_on_every_read(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER PRIMARY KEY, label TEXT) WITHOUT ROWID",
            f"{_SERIES} INSERT INTO plot SELECT gen.value, 'p' || gen.value FROM gen",
        )
        columns = adapter.introspect_columns("main.plot")
        scope = TableScope(sample=0.25)

        first, _ = adapter.compute_statistics(
            "main.plot",
            columns,
            StatisticsConfig(),
            frozenset(),
            scope=scope,
        )
        second, _ = adapter.compute_statistics(
            "main.plot",
            columns,
            StatisticsConfig(),
            frozenset(),
            scope=scope,
        )

        assert 0 < first.rows_scanned < 400
        assert first.rows_scanned == second.rows_scanned

    def test_the_copied_draw_keeps_its_columns_collation(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER PRIMARY KEY, label TEXT COLLATE NOCASE)",
            f"{_SERIES} INSERT INTO plot (label) "
            "SELECT CASE WHEN gen.value % 2 THEN 'A' ELSE 'a' END FROM gen",
        )
        columns = adapter.introspect_columns("main.plot")
        scope = adapter.materialize_scope("main.plot", TableScope(sample=0.5))

        _, stats = adapter.compute_statistics(
            "main.plot",
            columns,
            StatisticsConfig(),
            frozenset(),
            scope=scope,
        )

        assert stats["label"].cardinality == 1

    def test_the_copied_draw_of_a_strict_table_keeps_each_value_s_storage_class(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter(
            "CREATE TABLE plot (id INTEGER PRIMARY KEY, label ANY) STRICT",
            f"{_SERIES} INSERT INTO plot (label) "
            "SELECT CASE WHEN gen.value % 2 THEN 5 ELSE '5' END FROM gen",
        )
        adapter.introspect_columns("main.plot")
        scope = adapter.materialize_scope("main.plot", TableScope(sample=0.5))

        rows = adapter.execute_query(
            f"SELECT DISTINCT TYPEOF(label) FROM temp.{scope.materialized} ORDER BY 1",
        )

        assert rows == [("integer",), ("text",)]


class TestUserSql:
    def test_the_last_statement_answers_and_a_semicolon_in_a_literal_splits_nothing(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter("CREATE TABLE plot (label TEXT)", "INSERT INTO plot VALUES ('a;b')")

        rows = adapter.execute_query(
            "SELECT 1; -- a note; with a semicolon\nSELECT label FROM plot WHERE label = 'a;b';\n-- done",
        )

        assert rows == [("a;b",)]

    def test_a_statement_returning_no_rows_set_is_reported(
        self,
        open_adapter: Callable[..., SqliteAdapter],
    ) -> None:
        adapter = open_adapter("CREATE TABLE plot (label TEXT)")

        with pytest.raises(NoResultSet):
            adapter.execute_query("SELECT 1; CREATE TEMP TABLE scratch (a)")


_ENDLESS = (
    "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM cnt) SELECT COUNT(1) FROM cnt"
)


class TestAStatementEnds:
    def test_past_its_deadline_as_a_timed_out_failure(self) -> None:
        adapter = SqliteAdapter(
            {"database": ":memory:"},
            cursor_factory=lambda _p: _memory().cursor(),
            statement_timeout=1,
        )
        adapter.connect()

        try:
            with pytest.raises(QueryFailed) as caught:
                adapter.execute_query(_ENDLESS)

            assert adapter.execute_query("SELECT 42") == [(42,)]
        finally:
            adapter.close()

        assert caught.value.timed_out is True

    def test_on_a_ctrl_c_as_an_interrupt(self, armed: Callable[[], None]) -> None:
        adapter = SqliteAdapter(
            {"database": ":memory:"},
            cursor_factory=lambda _p: _memory().cursor(),
        )
        adapter.connect()
        previous = signal.signal(signal.SIGINT, signal.default_int_handler)
        armed()

        try:
            with pytest.raises(Interrupted):
                adapter.execute_query(_ENDLESS)
        finally:
            signal.signal(signal.SIGINT, previous)
            adapter.close()


class TestTheDrawsCountATableOncePerSession:
    """Every column's `looks_like` draw reuses the table's count and its kind lookup."""

    def test_a_session_counts_and_looks_up_the_table_once(self) -> None:
        connection = _memory(
            "CREATE TABLE plot (id INTEGER PRIMARY KEY, label TEXT, bed INTEGER)",
            f"{_SERIES} INSERT INTO plot SELECT gen.value, 'p' || (gen.value % 9), gen.value % 5 "
            "FROM gen",
        )
        statements: list[str] = []
        adapter = SqliteAdapter(
            {"database": ":memory:"},
            cursor_factory=lambda _p: _Recording(connection.cursor(), statements),
        )
        adapter.connect()
        adapter.list_tables(include=["*"], exclude=[])
        statements.clear()

        try:
            adapter.estimate_row_count("main.plot")

            for column in ("id", "label", "bed"):
                adapter.sample_values("main.plot", column, 10)
        finally:
            adapter.close()

        flattened = [" ".join(s.split()) for s in statements]

        assert flattened.count('SELECT COUNT(1) FROM "main"."plot" src') == 1
        assert sum("PRAGMA_TABLE_LIST()" in s for s in flattened) == 1


class _Recording:
    def __init__(self, cursor: sqlite3.Cursor, statements: list[str]) -> None:
        self._cursor = cursor
        self._statements = statements

    def __getattr__(self, name: str) -> object:
        return getattr(self._cursor, name)

    def execute(self, sql: str, params: Any = None) -> object:
        self._statements.append(sql)

        return self._cursor.execute(sql) if params is None else self._cursor.execute(sql, params)
