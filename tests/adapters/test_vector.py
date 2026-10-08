"""A vector column publishes its dimension, norm bounds and zero count, never an element."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from dbprint.adapters import ClickhouseAdapter, ColumnMeta, PostgresAdapter
from dbprint.adapters.dialect import Vendor
from dbprint.config.project import RedactRule
from tests.adapters._composites import generate, psql
from tests.adapters._dialects import STATS_MODULES, foreign_fragments
from tests.adapters._sql_style import alias_violations, layout_violations, violations


_VECTOR_TYPES: dict[str, str] = {
    "postgres": "vector(768)",
    "mysql": "vector(4)",
    "snowflake": "VECTOR(FLOAT, 8)",
    "clickhouse": "QBit(Float32, 8)",
}


@pytest.fixture
def embeddings(pgvector: object, postgres_test_db: dict[str, str]) -> dict[str, str]:
    del pgvector

    with psql(postgres_test_db) as conn:
        conn.execute("CREATE EXTENSION vector")
        conn.execute(
            "CREATE TABLE public.document (doc_id integer, unit vector(3), raw vector(3), "
            "mixed vector, pair vector(3))",
        )
        conn.execute(
            "INSERT INTO public.document SELECT i, "
            "ARRAY[0.6, 0.8, 0]::real[]::vector(3), "
            "ARRAY[i, 0, 0]::real[]::vector(3), "
            "CASE WHEN i % 2 = 0 THEN ARRAY[1, 2]::real[]::vector "
            "ELSE ARRAY[1, 2, 3, 4, 5]::real[]::vector END, "
            "CASE WHEN i <= 3 THEN ARRAY[0, 0, 0]::real[]::vector(3) "
            "ELSE ARRAY[i % 2, 1, 0]::real[]::vector(3) END "
            "FROM generate_series(1, 60) i",
        )
        conn.execute("CREATE VIEW public.document_view AS SELECT unit FROM public.document")

    return postgres_test_db


def test_pgvector_columns_publish_dimension_norm_and_zero_count(
    embeddings: dict[str, str],
    tmp_path: Path,
) -> None:
    columns = generate(PostgresAdapter(embeddings), "postgres", tmp_path, "*.document")

    assert columns["unit"]["classification"] == "vector"
    assert columns["unit"]["dimension"] == {"min": 3, "max": 3}
    assert columns["unit"]["norm"] == {"min": 1.0, "max": 1.0}
    assert columns["unit"]["zero_count"] == 0
    assert columns["raw"]["norm"] == {"min": 1.0, "max": 60.0}
    assert columns["mixed"]["dimension"] == {"min": 2, "max": 5}
    assert columns["pair"]["zero_count"] == 3
    assert columns["pair"]["norm"]["min"] == 1.0  # noqa: RUF069 - the expected value is an exact literal
    assert not {"cardinality", "values", "sketch", "redacted"} & set(columns["pair"])


def test_a_pgvector_type_off_the_search_path_is_still_a_vector(
    pgvector: object,
    postgres_test_db: dict[str, str],
    tmp_path: Path,
) -> None:
    del pgvector

    with psql(postgres_test_db) as conn:
        conn.execute("CREATE SCHEMA kit")
        conn.execute("CREATE EXTENSION vector SCHEMA kit")
        conn.execute("CREATE TABLE public.passage (passage_id integer, emb kit.vector(3))")
        conn.execute(
            "INSERT INTO public.passage SELECT i, ARRAY[i, 1, 0]::real[]::kit.vector(3) "
            "FROM generate_series(1, 30) i",
        )

    emb = generate(PostgresAdapter(postgres_test_db), "postgres", tmp_path, "*.passage")["emb"]

    assert emb["classification"] == "vector"
    assert not {"values", "cardinality", "sketch"} & set(emb)
    assert emb["dimension"] == {"min": 3, "max": 3}
    assert "unmeasured" not in emb


def test_a_redact_rule_leaves_a_vector_column_unmarked(
    embeddings: dict[str, str],
    tmp_path: Path,
) -> None:
    columns = generate(
        PostgresAdapter(embeddings),
        "postgres",
        tmp_path,
        "*.document",
        redact=(RedactRule(columns=("*.document.unit",), with_="mask"),),
    )

    assert "redacted" not in columns["unit"]
    assert columns["unit"]["norm"] == {"min": 1.0, "max": 1.0}


def test_a_view_over_a_vector_column_is_vector_by_type_alone(
    embeddings: dict[str, str],
    tmp_path: Path,
) -> None:
    columns = generate(PostgresAdapter(embeddings), "postgres", tmp_path, "*.document_view")

    assert columns["unit"] == {
        "sql_type": "vector(3)",
        "nullable": True,
        "classification": "vector",
    }


def test_a_clickhouse_qbit_reads_its_dimension_from_the_type(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.passage (passage_id UInt32, embedding QBit(Float32, 4)) "
        "ENGINE = Memory",
    )
    cursor.execute(
        "INSERT INTO seedbank.passage SELECT number, [3, 4, 0, 0] FROM numbers(1, 20)",
    )
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    columns = generate(adapter, "clickhouse", tmp_path, "*.passage")

    assert columns["embedding"]["classification"] == "vector"
    assert columns["embedding"]["dimension"] == {"min": 4, "max": 4}
    assert columns["embedding"]["norm"] == {"min": 5.0, "max": 5.0}


@pytest.mark.parametrize("vendor", sorted(_VECTOR_TYPES))
def test_the_vector_read_speaks_its_own_dialect(
    vendor: Vendor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (statement,) = _read(vendor, monkeypatch, STATS_MODULES[vendor])

    assert foreign_fragments(statement, vendor) == []
    assert violations(statement, vendor) + alias_violations(statement, vendor) == []
    assert layout_violations(statement, vendor) == []


def test_mysql_names_the_norm_and_zero_count_unmeasured(monkeypatch: pytest.MonkeyPatch) -> None:
    module = STATS_MODULES["mysql"]
    monkeypatch.setattr(module, "exec_query", lambda *_args: _Row((5, 4, 4, None, None, 0)))
    column = ColumnMeta(name="v", sql_type="vector(4)", nullable=True, default=None, ordinal=1)

    measured = module._fetch_vector(object(), "`t`", column)

    assert measured.dimension == (4, 4)
    assert measured.norm is None
    assert measured.zero_count is None
    assert measured.unmeasured == ("norm", "zero_count")


class _Row:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...]:
        return self._row


def _read(vendor: str, monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> list[str]:
    seen: list[str] = []

    def execute(_cursor: Any, sql: str, *_params: Any) -> _Row:
        seen.append(sql)

        return _Row((0, None, None, None, None, None))

    monkeypatch.setattr(module, "exec_query", execute)
    column = ColumnMeta(
        name="embedding",
        sql_type=_VECTOR_TYPES[vendor],
        nullable=True,
        default=None,
        ordinal=1,
    )

    if vendor == "snowflake":
        identity = SimpleNamespace(source_column=lambda name: f"src.{name}")
        module._fetch_vector(object(), identity, "src_table src", column)
    else:
        module._fetch_vector(object(), "src_table src", column)

    return seen
