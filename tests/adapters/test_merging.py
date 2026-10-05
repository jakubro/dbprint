"""A merging ClickHouse engine is stated as a `merging` block (SPEC 2.2.19), never measured."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from dbprint.adapters import ClickhouseAdapter
from dbprint.adapters.clickhouse import introspect
from dbprint.adapters.clickhouse.connection import DIALECT
from dbprint.adapters.identifiers import Identity
from dbprint.config.project import ConnectionConfig, RuleConfig
from dbprint.conformance import validate_print
from dbprint.engine import Engine
from dbprint.engine.context_assembler import AssemblyOptions, assemble


def test_a_replacing_table_states_its_key_and_counts_its_stored_rows(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.lot_state (lot_no UInt64, updated_at DateTime) "
        "ENGINE = ReplacingMergeTree(updated_at) ORDER BY lot_no",
    )
    cursor.execute("INSERT INTO seedbank.lot_state VALUES (1, '2026-01-01 00:00:00')")
    cursor.execute("INSERT INTO seedbank.lot_state VALUES (1, '2026-01-02 00:00:00')")
    statistics = _generate(cursor, tmp_path, "*.lot_state")

    assert statistics["row_count"] == 2
    assert statistics["merging"] == {
        "engine": "ReplacingMergeTree",
        "key": [{"expression": "lot_no", "column": "lot_no"}],
        "one_row_per_key": True,
        "rows": "stored",
    }
    assert not [k for k in statistics["grain"]["keys"] if k["detection"] == "declared"]


@pytest.mark.parametrize(
    ("engine", "order_by", "expected_key", "one_row_per_key"),
    [
        (
            "SummingMergeTree",
            "(plot_id, day)",
            [
                {"expression": "plot_id", "column": "plot_id"},
                {"expression": "day", "column": "day"},
            ],
            True,
        ),
        (
            "CollapsingMergeTree(sign)",
            "plot_id",
            [{"expression": "plot_id", "column": "plot_id"}],
            False,
        ),
        (
            "SummingMergeTree",
            "(cityHash64(plot_id, day), sign)",
            [{"expression": "cityHash64(plot_id, day)"}, {"expression": "sign", "column": "sign"}],
            True,
        ),
        ("ReplacingMergeTree", "tuple()", [], True),
    ],
)
def test_each_engine_states_its_family_and_its_ordered_key(
    clickhouse_native_connection: Any,
    tmp_path: Path,
    engine: str,
    order_by: str,
    expected_key: list[dict[str, str]],
    one_row_per_key: bool,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.plot_total (plot_id UInt64, day Date, sign Int8) "
        f"ENGINE = {engine} ORDER BY {order_by}",
    )
    cursor.execute("INSERT INTO seedbank.plot_total VALUES (1, '2026-01-01', 1)")
    merging = _generate(cursor, tmp_path, "*.plot_total")["merging"]

    assert merging["engine"] == engine.split("(")[0]
    assert merging["key"] == expected_key
    assert merging["one_row_per_key"] is one_row_per_key


def test_a_plain_merge_tree_carries_no_block(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute("CREATE TABLE seedbank.plain (id UInt64) ENGINE = MergeTree ORDER BY id")
    cursor.execute("INSERT INTO seedbank.plain VALUES (1)")

    assert "merging" not in _generate(cursor, tmp_path, "*.plain")


def test_a_session_reading_final_states_merged_rows(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.lot_state (lot_no UInt64, v UInt8) "
        "ENGINE = ReplacingMergeTree ORDER BY lot_no",
    )
    cursor.execute("INSERT INTO seedbank.lot_state VALUES (1, 1)")
    cursor.execute("INSERT INTO seedbank.lot_state VALUES (1, 2)")
    cursor.execute("SET final = 1")
    statistics = _generate(cursor, tmp_path, "*.lot_state")

    assert statistics["merging"]["rows"] == "merged"
    assert statistics["row_count"] == 1


def test_a_sampled_copy_states_stored_rows_under_a_final_session(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.lot_state (lot_no UInt64, v UInt8) ENGINE = ReplacingMergeTree "
        "ORDER BY (lot_no, sipHash64(lot_no)) SAMPLE BY sipHash64(lot_no)",
    )
    cursor.execute("INSERT INTO seedbank.lot_state SELECT number, 1 FROM numbers(200)")
    cursor.execute("SET final = 1")
    statistics = _generate(
        cursor,
        tmp_path,
        "*.lot_state",
        rules=(RuleConfig(include=("seedbank.lot_state",), sample=0.5),),
    )

    assert statistics["merging"]["rows"] == "stored"
    assert "scope" in statistics


def test_a_matview_states_its_inner_tables_engine(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.reading (plot_id UInt64) ENGINE = MergeTree ORDER BY plot_id",
    )
    cursor.execute(
        "CREATE MATERIALIZED VIEW seedbank.reading_count ENGINE = AggregatingMergeTree "
        "ORDER BY plot_id AS SELECT plot_id, countState() AS n FROM seedbank.reading GROUP BY plot_id",
    )
    cursor.execute("INSERT INTO seedbank.reading VALUES (1), (1), (2)")
    merging = _generate(cursor, tmp_path, "*.reading_count")["merging"]

    assert merging["engine"] == "AggregatingMergeTree"
    assert merging["key"] == [{"expression": "plot_id", "column": "plot_id"}]


def test_a_failed_read_names_the_block_unmeasured(
    clickhouse_native_connection: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.lot_state (lot_no UInt64) ENGINE = ReplacingMergeTree ORDER BY lot_no",
    )

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("system.tables refused")

    monkeypatch.setattr(introspect, "merging", refuse)
    statistics = _generate(cursor, tmp_path, "*.lot_state")

    assert "merging" not in statistics
    assert "merging" in statistics["unmeasured"]


def test_context_tells_the_query_writer_to_read_with_final(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.lot_state (lot_no UInt64) ENGINE = ReplacingMergeTree ORDER BY lot_no",
    )
    cursor.execute(
        "CREATE TABLE seedbank.visit (visit_id UInt64, sign Int8) "
        "ENGINE = CollapsingMergeTree(sign) ORDER BY visit_id",
    )
    _generate(cursor, tmp_path, "*.lot_state", "*.visit", count=2)
    query = _context(tmp_path, "seedbank.lot_state", AssemblyOptions(purpose="query"))
    profile = _context(tmp_path, "seedbank.visit", AssemblyOptions())

    assert (
        "Merging: ReplacingMergeTree on (lot_no) - rows counted here may repeat a key until "
        "merged; query with FINAL, or GROUP BY lot_no, for one row per key"
    ) in query
    assert (
        "Merging: CollapsingMergeTree on (visit_id) - rows counted here include state and "
        "cancel rows not yet collapsed; query with FINAL for the collapsed rows"
    ) in profile


def test_a_hand_edited_key_column_fails_conformance(
    clickhouse_native_connection: Any,
    tmp_path: Path,
) -> None:
    cursor = clickhouse_native_connection
    cursor.execute(
        "CREATE TABLE seedbank.lot_state (lot_no UInt64) ENGINE = ReplacingMergeTree ORDER BY lot_no",
    )
    _generate(cursor, tmp_path, "*.lot_state")
    path = next((tmp_path / "primary").rglob("statistics.yaml"))
    data = yaml.safe_load(path.read_text())
    data["merging"]["key"][0]["column"] = "absent_id"
    path.write_text(yaml.safe_dump(data))

    assert "stats.merging-unknown-column" in {i.code for i in validate_print(tmp_path / "primary")}


@pytest.mark.parametrize(
    ("engine", "expected"),
    [
        ("ReplicatedReplacingMergeTree", "ReplacingMergeTree"),
        ("SharedVersionedCollapsingMergeTree", "VersionedCollapsingMergeTree"),
        ("SharedMergeTree", None),
        ("GraphiteMergeTree", None),
    ],
)
def test_a_replication_prefix_is_not_part_of_the_family(engine: str, expected: str | None) -> None:
    merging = introspect.merging(_RowCursor((engine, "k", "")), _IDENTITY, final=False)

    assert (merging.engine if merging is not None else None) == expected


class _RowCursor:
    def __init__(self, row: tuple[Any, ...]) -> None:
        self._row = row

    def execute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def fetchone(self) -> tuple[Any, ...]:
        return self._row

    def fetchall(self) -> list[tuple[Any, ...]]:
        return [self._row]

    def close(self) -> None:
        return None


_IDENTITY = Identity.of(("seedbank", "lot_state"), DIALECT)


def _context(tmp_path: Path, fqn: str, options: AssemblyOptions) -> str:
    root = tmp_path / "primary"
    manifest = yaml.safe_load((root / "manifest.yaml").read_text())

    return assemble(manifest, root, [fqn], options).text


def _generate(
    cursor: Any,
    tmp_path: Path,
    *include: str,
    count: int = 1,
    rules: tuple[RuleConfig, ...] = (),
) -> dict[str, Any]:
    adapter = ClickhouseAdapter(
        {"host": "chdb", "database": "seedbank"},
        cursor_factory=lambda _params: cursor,
    )
    conn = ConnectionConfig(
        name="primary",
        adapter="clickhouse",
        output=tmp_path,
        include=include,
        rules=rules,
    )

    try:
        Engine(adapter, conn, tmp_path).generate()
    finally:
        adapter.close()

    written = sorted((tmp_path / "primary").rglob("statistics.yaml"))
    errors = [i for i in validate_print(tmp_path / "primary") if i.severity == "error"]

    assert errors == [], errors
    assert len(written) == count

    return yaml.safe_load(written[0].read_text())
