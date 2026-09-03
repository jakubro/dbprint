"""ClickHouse addresses `system.*` case-sensitively, so a folded FQN resolves to nothing.

Nothing above column grain carries a physical spelling, so an identifier SPEC 1.5 cannot spell is
refused (SPEC 1.5.5) rather than lowercased into a path addressing no table.
"""

from __future__ import annotations

from typing import Any

import pytest

from dbprint.adapters.clickhouse.introspect import IdentifierRejected, list_tables


class _Cursor:
    """Returns one `system.tables` row per fixture name; records nothing else."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def execute(self, sql: str, params: Any = None) -> None:
        del sql, params

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> Any:
        """Part of the `Cursor` protocol; `list_tables` never calls it."""

        return None

    def close(self) -> None:
        """Part of the `Cursor` protocol; this fixture holds nothing to release."""


def _list(database: str, *names: str) -> list[str]:
    cursor = _Cursor([(name, "MergeTree", "") for name in names])
    metas, _samplable = list_tables(cursor, database, include=["*"], exclude=[])

    return [meta.fqn for meta in metas]


class TestLowercaseIdentifiersPassThroughUnchanged:
    def test_the_fqn_is_the_catalog_spelling(self) -> None:
        assert _list("seedbank", "accession") == ["seedbank.accession"]

    def test_digits_and_underscores_are_legal_segments(self) -> None:
        assert _list("seedbank_2", "accession_v2") == ["seedbank_2.accession_v2"]


class TestACapitalIsRefusedRatherThanFolded:
    def test_a_capitalised_database_is_rejected(self) -> None:
        with pytest.raises(IdentifierRejected, match="contains-unsafe-character"):
            _list("Seedbank", "accession")

    def test_a_capitalised_table_is_rejected(self) -> None:
        with pytest.raises(IdentifierRejected, match="contains-unsafe-character"):
            _list("seedbank", "Accession")

    def test_the_message_names_the_catalog_spelling_a_user_would_search_for(self) -> None:
        """SPEC 1.5.5's resolution is to rename or exclude the identifier, so the message has to
        carry the name the database actually holds - a folded one matches no selector.
        """

        with pytest.raises(IdentifierRejected) as excinfo:
            _list("Seedbank", "accession")

        assert "Seedbank.accession" in str(excinfo.value)

    def test_an_excluded_capital_does_not_reject_the_connection(self) -> None:
        """The refusal applies to what this run selected, so the documented resolution works."""

        cursor = _Cursor([("Accession", "MergeTree", ""), ("vault", "MergeTree", "")])
        metas, _samplable = list_tables(
            cursor,
            "seedbank",
            include=["*"],
            exclude=["seedbank.Accession"],
        )

        assert [meta.fqn for meta in metas] == ["seedbank.vault"]


class TestALeadingPeriodIsStillRefused:
    def test_a_hidden_storage_name_is_rejected_by_its_own_reason(self) -> None:
        with pytest.raises(IdentifierRejected, match="leading-period"):
            _list("seedbank", ".hidden")
