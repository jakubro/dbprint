"""ClickHouse folds identifiers into the path and binds the catalog's own spelling.

`system.*` compares case-sensitively, so every read past enumeration carries the physical spelling.
"""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.adapters import ClickhouseAdapter, StatisticsConfig
from dbprint.adapters.clickhouse.identity import Identity
from dbprint.adapters.clickhouse.introspect import IdentifierRejected, columns, list_tables


class _Cursor:
    """Returns one `system.tables` row per fixture name and records every statement bound."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.bound: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.bound.append((" ".join(sql.split()), params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> Any:
        """Part of the `Cursor` protocol; the reads under test here fetch all rows."""

        return self._rows[0] if self._rows else None

    def close(self) -> None:
        """Part of the `Cursor` protocol; this fixture holds nothing to release."""


def _enumerate(database: str, *names: str) -> tuple[list[str], _Cursor, dict[str, tuple[str, str]]]:
    """List `names` in `database` and hand back the paths, the cursor and the physical map."""

    cursor = _Cursor([(name, "MergeTree", "") for name in names])
    metas, _samplable, physical = list_tables(cursor, database, include=["*"], exclude=[])

    return [meta.fqn for meta in metas], cursor, physical


class TestTheCatalogSpellingFoldsIntoThePath:
    def test_a_lowercase_name_is_unchanged(self) -> None:
        paths, _cursor, _physical = _enumerate("seedbank", "accession")

        assert paths == ["seedbank.accession"]

    def test_digits_and_underscores_are_legal_segments(self) -> None:
        paths, _cursor, _physical = _enumerate("seedbank_2", "accession_v2")

        assert paths == ["seedbank_2.accession_v2"]

    def test_a_capitalised_database_folds(self) -> None:
        paths, _cursor, _physical = _enumerate("Seedbank", "accession")

        assert paths == ["seedbank.accession"]

    def test_a_capitalised_table_folds(self) -> None:
        paths, _cursor, _physical = _enumerate("seedbank", "Accession")

        assert paths == ["seedbank.accession"]

    def test_the_namespace_path_folds_with_the_fqn(self) -> None:
        """The path segments are what the artifact is written under (SPEC 1.3)."""

        cursor = _Cursor([("Accession", "MergeTree", "")])
        metas, _samplable, _physical = list_tables(cursor, "Seedbank", include=["*"], exclude=[])

        assert metas[0].namespace_path == ("seedbank", "accession")


class TestTheCatalogSpellingSurvivesForTheStatements:
    """Folding without a carrier would address a table `system.*` does not have."""

    def test_enumeration_hands_back_the_physical_pair(self) -> None:
        _paths, _cursor, physical = _enumerate("Seedbank", "Accession")

        assert physical == {"seedbank.accession": ("Seedbank", "Accession")}

    def test_a_catalog_read_binds_the_native_spelling(self) -> None:
        cursor = _Cursor([("Id", "UUID", 1, "")])

        columns(cursor, Identity(parts=("Seedbank", "Accession")))

        _sql, params = cursor.bound[-1]
        assert params == ("Seedbank", "Accession")

    def test_a_column_keeps_its_catalog_spelling(self) -> None:
        cursor = _Cursor([("Id", "UUID", 1, "")])

        metas, physical = columns(cursor, Identity(parts=("seedbank", "accession")))

        assert metas[0].name == "id"
        assert metas[0].physical_name == "Id"
        assert physical == {"id": "Id"}

    def test_a_data_statement_quotes_the_column_the_catalog_holds(self) -> None:
        identity = Identity(parts=("Seedbank", "Accession"), columns={"id": "Id"})

        assert identity.quoted() == "`Seedbank`.`Accession`"
        assert identity.quoted_column("id") == "`Id`"


class TestTwoSpellingsOfOneNameAreRefused:
    """SPEC 1.5.2: they collapse onto one path, so one would overwrite the other."""

    def test_a_case_collision_rejects_the_run(self) -> None:
        with pytest.raises(IdentifierRejected, match="case-collides-with"):
            _enumerate("seedbank", "accession", "Accession")

    def test_the_message_names_both_spellings(self) -> None:
        with pytest.raises(IdentifierRejected) as excinfo:
            _enumerate("seedbank", "accession", "Accession")

        message = str(excinfo.value)
        assert "seedbank.accession" in message
        assert "seedbank.Accession" in message

    def test_excluding_the_folded_path_removes_both_and_the_run_proceeds(self) -> None:
        """Selectors are applied before the rules, so the documented resolution works."""

        cursor = _Cursor(
            [(name, "MergeTree", "") for name in ("accession", "Accession", "vault")],
        )
        metas, _samplable, _physical = list_tables(
            cursor,
            "seedbank",
            include=["*"],
            exclude=["seedbank.accession"],
        )

        assert [meta.fqn for meta in metas] == ["seedbank.vault"]


class TestAnIdentifierTheFormatCannotSpellIsStillRefused:
    """Judged on the folded form (SPEC 1.5.1), so only a genuinely disallowed character bites."""

    def test_a_space_is_rejected(self) -> None:
        with pytest.raises(IdentifierRejected, match="contains-unsafe-character"):
            _enumerate("seedbank", "field notes")

    def test_a_hidden_storage_name_is_rejected_by_its_own_reason(self) -> None:
        with pytest.raises(IdentifierRejected, match="leading-period"):
            _enumerate("seedbank", ".hidden")

    def test_the_resolution_quotes_the_path_an_exclude_would_match(self) -> None:
        """A selector matches the lowercased FQN, so a native-cased one would match nothing."""

        with pytest.raises(IdentifierRejected) as excinfo:
            _enumerate("Seedbank", "field notes")

        assert 'exclude:\n      - "seedbank.field notes"' in str(excinfo.value)

    def test_a_lowercase_exclude_matches_a_capitalised_table(self) -> None:
        cursor = _Cursor([(name, "MergeTree", "") for name in ("Accession", "vault")])
        metas, _samplable, _physical = list_tables(
            cursor,
            "seedbank",
            include=["*"],
            exclude=["seedbank.accession"],
        )

        assert [meta.fqn for meta in metas] == ["seedbank.vault"]


class TestAgainstTheLiveEngine:
    """chdb is a real ClickHouse engine, so these prove the addressing, not just the text."""

    def test_a_capitalised_table_profiles(self, clickhouse_native_connection: Any) -> None:
        cursor = clickhouse_native_connection
        cursor.execute(
            "CREATE TABLE seedbank.Accession (id UUID, label String) "
            "ENGINE = MergeTree ORDER BY id",
        )
        cursor.execute(
            "INSERT INTO seedbank.Accession (id, label) VALUES "
            "('00000000-0000-7000-8000-000000000001', 'alpha'), "
            "('00000000-0000-7000-8000-000000000002', 'beta')",
        )
        adapter = ClickhouseAdapter(
            {"host": "chdb", "database": "seedbank"},
            cursor_factory=lambda _p: cursor,
        )
        adapter.connect()

        try:
            table = next(
                t
                for t in adapter.list_tables(include=["*"], exclude=[])
                if t.fqn == "seedbank.accession"
            )
            columns_read = adapter.introspect_columns(table.fqn)
            counts, _base = adapter.compute_base_statistics(
                table.fqn,
                columns_read,
                StatisticsConfig(),
            )
        finally:
            adapter.close()

        assert [c.name for c in columns_read] == ["id", "label"]
        assert counts.row_count == 2

    def test_a_capitalised_database_profiles(self, clickhouse_native_connection: Any) -> None:
        cursor = clickhouse_native_connection
        cursor.execute("CREATE DATABASE Seedbank")
        cursor.execute("CREATE TABLE Seedbank.vault (id UUID) ENGINE = MergeTree ORDER BY id")
        cursor.execute(
            "INSERT INTO Seedbank.vault (id) VALUES ('00000000-0000-7000-8000-000000000001')",
        )
        adapter = ClickhouseAdapter(
            {"host": "chdb", "database": "Seedbank"},
            cursor_factory=lambda _p: cursor,
        )
        adapter.connect()

        try:
            tables = adapter.list_tables(include=["*"], exclude=[])
            ddl = adapter.extract_ddl("seedbank.vault")
        finally:
            adapter.close()

        assert [t.fqn for t in tables] == ["seedbank.vault"]
        assert "vault" in ddl

    def test_the_pair_probes_address_the_mixed_case_column_too(
        self,
        clickhouse_native_connection: Any,
    ) -> None:
        """The probes take candidates as folded map keys, so they resolve each back to its spelling.

        A refused statement here is swallowed by the orchestrator, so nothing downstream reports it.
        """

        cursor = clickhouse_native_connection
        cursor.execute(
            "CREATE TABLE seedbank.pair_probe (id UUID, seedCount Int32, vaultRef Int32) "
            "ENGINE = MergeTree ORDER BY id",
        )
        cursor.execute(
            "INSERT INTO seedbank.pair_probe (id, seedCount, vaultRef) VALUES "
            "('00000000-0000-7000-8000-000000000001', 30, 1), "
            "('00000000-0000-7000-8000-000000000002', 31, 2)",
        )
        adapter = ClickhouseAdapter(
            {"host": "chdb", "database": "seedbank"},
            cursor_factory=lambda _p: cursor,
        )
        adapter.connect()
        pair = (("seedcount", "vaultref"),)

        try:
            adapter.list_tables(include=["*"], exclude=[])
            read = adapter.introspect_columns("seedbank.pair_probe")
            counts, base = adapter.compute_base_statistics(
                "seedbank.pair_probe",
                read,
                StatisticsConfig(),
            )
            grain = adapter.probe_grain("seedbank.pair_probe", read, counts, pair)
            dependencies = adapter.probe_dependencies(
                "seedbank.pair_probe",
                read,
                counts,
                base,
                pair,
            )
        finally:
            adapter.close()

        assert grain == pair
        assert dependencies[("seedcount", "vaultref")] == 1.0

    def test_a_mixed_case_column_is_addressed_by_its_catalog_spelling(
        self,
        clickhouse_native_connection: Any,
    ) -> None:
        cursor = clickhouse_native_connection
        cursor.execute(
            "CREATE TABLE seedbank.mixed_column (id UUID, seedCount Int32) "
            "ENGINE = MergeTree ORDER BY id",
        )
        cursor.execute(
            "INSERT INTO seedbank.mixed_column (id, seedCount) VALUES "
            "('00000000-0000-7000-8000-000000000001', 30), "
            "('00000000-0000-7000-8000-000000000002', 31)",
        )
        adapter = ClickhouseAdapter(
            {"host": "chdb", "database": "seedbank"},
            cursor_factory=lambda _p: cursor,
        )
        adapter.connect()
        config = StatisticsConfig(enumeration_threshold=1)

        try:
            adapter.list_tables(include=["*"], exclude=[])  # captures the physical spellings
            read = adapter.introspect_columns("seedbank.mixed_column")
            _counts, stats = adapter.compute_statistics(
                "seedbank.mixed_column",
                read,
                config,
                frozenset(),
            )
            sampled = adapter.sample_values("seedbank.mixed_column", "seedcount", n=5)
        finally:
            adapter.close()

        assert {c.name: c.physical_name for c in read}["seedcount"] == "seedCount"
        assert stats["seedcount"].cardinality == 2
        assert sorted(sampled) == [30, 31]
